"""OpenLess Qwen-style PCM16 stream -> official Realtime -> prepared text.

The upstream model creates one text-only response after recording finishes.
Final client events are sent before upstream connection cleanup.
"""
import asyncio
import audioop
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timezone

from aiohttp import web, ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, ClientError
from credential_store import STATE
from openai_credentials import load_key, key_status
from realtime_profile import prompt_snapshot
from cursor_context import begin_capture, policy as cursor_policy

MODEL = 'gpt-realtime-2.1'
UPSTREAM = 'wss://api.openai.com/v1/realtime?model=' + MODEL
MAX_AUDIO_BYTES = 32000 * 300
LAST_RESULT = None


class RealtimeFailure(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(code)
        self.details = {'code': code, 'message': message, 'retryable': retryable}


def upstream_failure(error):
    code = str(error.get('code') or error.get('type') or '') if isinstance(error, dict) else ''
    if code in ('invalid_api_key', 'authentication_error', 'permission_denied'):
        return RealtimeFailure('openai_auth_failed', 'OpenAI API 密钥不可用或权限不足，请检查本机密钥。')
    if code in ('insufficient_quota', 'credit_balance_exhausted', 'billing_hard_limit_reached'):
        return RealtimeFailure('openai_quota_exhausted', 'OpenAI API 额度不足，请检查额度或预算限制。')
    if 'rate_limit' in code:
        return RealtimeFailure('openai_rate_limited', 'OpenAI 请求过于频繁，请稍后重试。', True)
    return RealtimeFailure('openai_request_failed', 'OpenAI Realtime 请求失败，请检查模型权限、网络或服务状态。', True)


def public_failure(exc):
    if isinstance(exc, RealtimeFailure):
        return exc.details
    if isinstance(exc, TimeoutError):
        return {'code': 'realtime_timeout', 'message': 'Realtime 响应超时，请重试或切回豆包通道。', 'retryable': True}
    if isinstance(exc, (ClientError, ConnectionError)):
        return {'code': 'realtime_network_error', 'message': 'OpenAI Realtime 连接失败，请检查网络后重试。', 'retryable': True}
    return {'code': 'realtime_failed', 'message': 'Realtime 语音处理失败，请检查服务状态后重试。', 'retryable': True}


def record_metrics(value):
    global LAST_RESULT
    value = dict(value, time_utc=datetime.now(timezone.utc).isoformat())
    LAST_RESULT = value
    STATE.mkdir(exist_ok=True)
    logger = logging.getLogger('openless.realtime.metrics')
    if not logger.handlers:
        handler = RotatingFileHandler(STATE / 'realtime-diagnostics.jsonl', maxBytes=1_000_000, backupCount=1, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    logger.info(json.dumps(value, ensure_ascii=False))


def health_status():
    return {'model': MODEL, 'key_status': key_status(), 'last_result': LAST_RESULT,
            'audio_input': 'streaming', 'output': 'prepared_text', 'reasoning': 'low',
            'cursor_context': cursor_policy()}


class RealtimeEngine:
    def __init__(self, *, upstream=UPSTREAM, key_loader=load_key, prompt_loader=prompt_snapshot,
                 cursor_factory=begin_capture):
        self.upstream, self.key_loader, self.prompt_loader = upstream, key_loader, prompt_loader
        self.cursor_factory = cursor_factory

    async def run(self, stream, ready, partial, completed):
        try:
            key = self.key_loader()
        except Exception:
            raise RealtimeFailure('openai_key_missing', '本机 OpenAI API 密钥缺失或无法解密，请重新配置。') from None
        prompt, prompt_meta = self.prompt_loader()
        start = time.monotonic()
        metrics = {'model': MODEL, 'status': 'cancelled', 'audio_bytes': 0, 'output_chars': 0,
                   'dictionary_entries': prompt_meta.get('dictionary_entries', 0),
                   'history_turns': prompt_meta.get('history_turns', 0)}
        pieces = []
        commit_time = first_time = None
        sender = receiver = ws = None
        cursor = None
        try:
            try:
                cursor = self.cursor_factory()
            except Exception:
                pass  # Optional capture must not break an otherwise valid recording.
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=4), trust_env=True) as session:
                async with asyncio.timeout(4.5):
                    try:
                        ws = await session.ws_connect(self.upstream, headers={'Authorization': 'Bearer ' + key},
                                heartbeat=20, timeout=ClientWSTimeout(ws_close=.5), max_msg_size=8_000_000, compress=0)
                    except ClientError as exc:
                        status = getattr(exc, 'status', None)
                        if status in (401, 403):
                            raise RealtimeFailure('openai_auth_failed', 'OpenAI API 密钥不可用或没有模型权限。') from None
                        if status == 429:
                            raise RealtimeFailure('openai_rate_or_quota', 'OpenAI API 额度或速率受限，请检查额度后重试。', True) from None
                        raise
                    first = await ws.receive_json()
                    if first.get('type') == 'error':
                        raise upstream_failure(first.get('error'))
                    if cursor is not None:
                        try:
                            block = cursor.take_ready()
                        except Exception:
                            block = None
                        if block:
                            prompt += '\n\n' + block
                    await ws.send_json({'type': 'session.update', 'session': {
                        'type': 'realtime', 'output_modalities': ['text'], 'instructions': prompt,
                        'reasoning': {'effort': 'low'}, 'max_output_tokens': 4096,
                        'audio': {'input': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'turn_detection': None}},
                        'tools': [],
                    }})
                    while True:
                        event = await ws.receive_json()
                        if event.get('type') == 'error':
                            raise upstream_failure(event.get('error'))
                        if event.get('type') == 'session.updated':
                            break
                await ready()
                metrics['ready_s'] = round(time.monotonic() - start, 3)

                async def send_audio():
                    nonlocal commit_time
                    state = None
                    nonzero = False
                    async for chunk in stream:
                        metrics['audio_bytes'] += len(chunk)
                        nonzero = nonzero or audioop.max(chunk, 2) != 0
                        converted, state = audioop.ratecv(chunk, 2, 1, 16000, 24000, state)
                        if converted:
                            await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(converted).decode('ascii')})
                    if metrics['audio_bytes'] < 3200:
                        raise RealtimeFailure('audio_too_short', '录音太短，请再说一次。', True)
                    if not nonzero:
                        raise RealtimeFailure('silent_audio', '没有收到有效录音，请检查麦克风。', True)
                    await ws.send_json({'type': 'input_audio_buffer.commit'})
                    commit_time = time.monotonic()
                    await ws.send_json({'type': 'response.create', 'response': {'output_modalities': ['text']}})

                sender = asyncio.create_task(send_audio())
                while True:
                    if sender.done():
                        sender.result()
                    receiver = asyncio.create_task(ws.receive())
                    done, _ = await asyncio.wait({sender, receiver} if not sender.done() else {receiver}, return_when=asyncio.FIRST_COMPLETED)
                    if sender in done:
                        try:
                            sender.result()
                        except BaseException:
                            receiver.cancel()
                            await asyncio.gather(receiver, return_exceptions=True)
                            raise
                    if receiver not in done:
                        message = await receiver
                    else:
                        message = receiver.result()
                    if message.type != WSMsgType.TEXT:
                        raise RealtimeFailure('realtime_upstream_closed', 'Realtime 在完整结果返回前断开，请重试。', True)
                    event = json.loads(message.data)
                    kind = event.get('type')
                    if kind == 'error':
                        raise upstream_failure(event.get('error'))
                    if kind in ('response.output_text.delta', 'response.text.delta') and event.get('delta'):
                        if first_time is None:
                            first_time = time.monotonic()
                        pieces.append(event['delta'])
                        await partial(''.join(pieces))
                    if kind in ('response.output_text.done', 'response.text.done') and not pieces and event.get('text'):
                        first_time = time.monotonic()
                        pieces.append(event['text'])
                    if kind == 'response.done':
                        response = event.get('response', {})
                        if response.get('status') != 'completed' or not ''.join(pieces).strip():
                            raise RealtimeFailure('realtime_incomplete', 'Realtime 未返回完整文字，请重试。', True)
                        text = ''.join(pieces).strip()
                        metrics.update(status='ok', output_chars=len(text),
                            after_commit_s=round(time.monotonic() - commit_time, 3) if commit_time is not None else None,
                            first_after_commit_s=round(first_time - commit_time, 3) if first_time is not None and commit_time is not None else None)
                        usage = response.get('usage', {})
                        metrics['input_tokens'] = usage.get('input_tokens')
                        metrics['output_tokens'] = usage.get('output_tokens')
                        await completed(text)
                        await ws.close()
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            metrics.update(status='error', error_code=public_failure(exc)['code'])
            raise
        finally:
            pending = [task for task in (sender, receiver) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if ws is not None:
                try:
                    await asyncio.wait_for(ws.close(), 1)
                except (TimeoutError, ClientError, ConnectionError):
                    pass
            if cursor is not None:
                try:
                    await cursor.close()
                    metrics['cursor_context'] = dict(cursor.metadata)
                except Exception:
                    metrics['cursor_context'] = {'status': 'unavailable', 'forwarded': False}
            metrics['elapsed_s'] = round(time.monotonic() - start, 3)
            try:
                record_metrics(metrics)
            except OSError:
                pass  # Optional diagnostics must not change a recognition result.


async def handle(request, lock, engine=None):
    if request.headers.get('Origin'):
        return web.json_response({'error': 'Browser requests disabled'}, status=403)
    if lock.locked():
        return web.json_response({'error': 'One request at a time'}, status=429)
    async with lock:
        client = web.WebSocketResponse(max_msg_size=100_000, heartbeat=20)
        await client.prepare(request)
        queue = asyncio.Queue(maxsize=100)
        finished = asyncio.Event()
        task = None
        count = 0

        async def audio():
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield chunk

        async def ready():
            # The local stream is acknowledged immediately. Its bounded queue
            # preserves microphone frames while the upstream connection opens.
            return

        async def partial(text):
            await client.send_json({'type': 'conversation.item.input_audio_transcription.text', 'text': text})

        async def completed(text):
            await client.send_json({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': text})
            await client.send_json({'type': 'session.finished'})
            finished.set()

        async def worker():
            try:
                await (engine or RealtimeEngine()).run(audio(), ready, partial, completed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not client.closed:
                    await client.send_json({'type': 'conversation.item.input_audio_transcription.failed', 'error': public_failure(exc)})
                finished.set()
                await client.close()

        try:
            async with asyncio.timeout(330):
                async for message in client:
                    if message.type != WSMsgType.TEXT:
                        break
                    event = json.loads(message.data)
                    kind = event.get('type')
                    if kind == 'session.update' and task is None:
                        task = asyncio.create_task(worker())
                        await client.send_json({'type': 'session.updated'})
                    elif kind == 'input_audio_buffer.append' and task is not None:
                        if task.done():
                            await task
                            break
                        chunk = base64.b64decode(event['audio'], validate=True)
                        count += len(chunk)
                        if not chunk or len(chunk) % 2 or count > MAX_AUDIO_BYTES:
                            raise RealtimeFailure('bad_audio', '请检查录音格式或五分钟长度限制。')
                        await asyncio.wait_for(queue.put(chunk), 5)
                    elif kind == 'session.finish' and task is not None:
                        await asyncio.wait_for(queue.put(None), 5)
                        await asyncio.wait_for(finished.wait(), 10)
                        break
                    else:
                        raise RealtimeFailure('bad_request', '本机语音协议顺序或字段不正确。')
        except Exception as exc:
            if not client.closed:
                await client.send_json({'type': 'conversation.item.input_audio_transcription.failed', 'error': public_failure(exc)})
        finally:
            if task:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()
        return client
