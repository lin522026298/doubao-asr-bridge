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
from realtime_profile import prompt_snapshot, SETTINGS as REALTIME_SETTINGS
from cursor_context import begin_capture, policy as cursor_policy
from realtime_text import normalize_output

MODEL = 'gpt-realtime-2.1'
UPSTREAM = 'wss://api.openai.com/v1/realtime?model=' + MODEL
MAX_AUDIO_BYTES = 32000 * 300
DEFAULT_REASONING = 'low'
REASONING_EFFORTS = frozenset(('minimal', 'low', 'medium', 'high', 'xhigh'))
# Accepted by a targeted gpt-realtime-2.1 probe, but outside the stable schema.
# This opt-in does not guarantee the model performs no internal reasoning.
EXPERIMENTAL_REASONING_EFFORTS = frozenset(('none',))
COMPAT_REASONING_FALLBACK = 'low'
EFFORT_SCHEMA_ERRORS = frozenset(('invalid_value', 'unsupported_value', 'invalid_parameter',
                                 'invalid_enum_value', 'unsupported_parameter_value'))
EFFORT_ERROR_PARAMS = frozenset(('session.reasoning.effort', 'reasoning.effort'))
LAST_RESULT = None
ACTIVE_REASONING = None


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


def load_realtime_settings():
    try:
        with REALTIME_SETTINGS.open('rb') as source:
            raw = source.read(8_000_001)
    except FileNotFoundError:
        return {}
    if len(raw) > 8_000_000:
        raise ValueError('Realtime settings exceed the size limit')
    return json.loads(raw)


def reasoning_snapshot(settings_loader=load_realtime_settings):
    """Return only safe diagnostics; never retain raw settings or loader errors."""
    try:
        config = settings_loader()
        if not isinstance(config, dict):
            return {'effort': DEFAULT_REASONING, 'status': 'invalid_fallback',
                    'reason': 'invalid_settings'}
        if 'reasoning' not in config:
            return {'effort': DEFAULT_REASONING, 'status': 'default'}
        value = config['reasoning']
        effort = value.strip().lower() if isinstance(value, str) else None
        if effort in REASONING_EFFORTS:
            return {'effort': effort, 'status': 'configured'}
        if effort in EXPERIMENTAL_REASONING_EFFORTS:
            return {'effort': effort, 'status': 'configured_experimental',
                    'compat_fallback_enabled': config.get('reasoning_compat_fallback', True) is True}
    except Exception:
        return {'effort': DEFAULT_REASONING, 'status': 'unavailable_fallback',
                'reason': 'settings_read_failed'}
    return {'effort': DEFAULT_REASONING, 'status': 'invalid_fallback',
            'reason': 'unsupported_effort'}


def effort_schema_rejection(error):
    """Recognize only a precise effort-field rejection, without reading its message."""
    if not isinstance(error, dict):
        return None
    code, param, kind = error.get('code'), error.get('param'), error.get('type')
    if not isinstance(code, str) or code not in EFFORT_SCHEMA_ERRORS:
        return None
    if not isinstance(param, str) or param not in EFFORT_ERROR_PARAMS:
        return None
    if kind is not None and kind not in ('invalid_request_error', 'validation_error', 'schema_error', *EFFORT_SCHEMA_ERRORS):
        return None
    return {'code': code, 'param': param}


def health_status(*, settings_loader=load_realtime_settings):
    configured = reasoning_snapshot(settings_loader)
    active = ACTIVE_REASONING
    return {'model': MODEL, 'key_status': key_status(), 'last_result': LAST_RESULT,
            'audio_input': 'streaming', 'output': 'prepared_text',
            'reasoning': (active or configured)['effort'],
            'reasoning_source': 'active_recording' if active is not None else 'next_recording',
            'reasoning_config': configured,
            'active_reasoning': dict(active) if active is not None else None,
            'cursor_context': cursor_policy()}


class RealtimeEngine:
    def __init__(self, *, upstream=UPSTREAM, key_loader=load_key, prompt_loader=prompt_snapshot,
                 cursor_factory=begin_capture, settings_loader=load_realtime_settings):
        self.upstream, self.key_loader, self.prompt_loader = upstream, key_loader, prompt_loader
        self.cursor_factory = cursor_factory
        self.settings_loader = settings_loader

    async def run(self, stream, ready, partial, completed):
        global ACTIVE_REASONING
        reasoning = reasoning_snapshot(self.settings_loader)
        active_reasoning = dict(reasoning, configured_effort=reasoning['effort'], session_accepted=False)
        try:
            key = self.key_loader()
        except Exception:
            raise RealtimeFailure('openai_key_missing', '本机 OpenAI API 密钥缺失或无法解密，请重新配置。') from None
        prompt, prompt_meta = self.prompt_loader()
        start = time.monotonic()
        metrics = {'model': MODEL, 'status': 'cancelled', 'audio_bytes': 0, 'output_chars': 0,
                   'reasoning': reasoning['effort'], 'reasoning_config': dict(reasoning),
                   'reasoning_sent': [], 'reasoning_session_accepted': None,
                   'reasoning_fallback': None,
                   'dictionary_entries': prompt_meta.get('dictionary_entries', 0),
                   'history_turns': prompt_meta.get('history_turns', 0)}
        pieces = []
        commit_time = first_time = None
        sender = receiver = ws = None
        cursor = None
        try:
            ACTIVE_REASONING = active_reasoning
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
                    session_update = {'type': 'session.update', 'session': {
                        'type': 'realtime', 'output_modalities': ['text'], 'instructions': prompt,
                        'reasoning': {'effort': reasoning['effort']}, 'max_output_tokens': 4096,
                        'audio': {'input': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'turn_detection': None}},
                        'tools': [],
                    }}
                    await ws.send_json(session_update)
                    metrics['reasoning_sent'].append(reasoning['effort'])
                    while True:
                        event = await ws.receive_json()
                        if event.get('type') == 'error':
                            rejection = effort_schema_rejection(event.get('error'))
                            if (reasoning['effort'] == 'none' and reasoning.get('compat_fallback_enabled')
                                    and len(metrics['reasoning_sent']) == 1 and rejection is not None):
                                # Still inside the original setup deadline. No audio sender
                                # or response exists before session.updated acknowledges it.
                                session_update['session']['reasoning']['effort'] = COMPAT_REASONING_FALLBACK
                                await ws.send_json(session_update)
                                metrics['reasoning_sent'].append(COMPAT_REASONING_FALLBACK)
                                metrics['reasoning'] = COMPAT_REASONING_FALLBACK
                                metrics['reasoning_fallback'] = {
                                    'from': 'none', 'to': COMPAT_REASONING_FALLBACK,
                                    'reason': 'schema_effort_rejected',
                                    'error_code': rejection['code'], 'error_param': rejection['param']}
                                active_reasoning.update(effort=COMPAT_REASONING_FALLBACK,
                                    status='compatibility_fallback', fallback_reason='schema_effort_rejected',
                                    fallback_error_code=rejection['code'], fallback_error_param=rejection['param'])
                                continue
                            raise upstream_failure(event.get('error'))
                        if event.get('type') == 'session.updated':
                            metrics['reasoning_session_accepted'] = metrics['reasoning']
                            active_reasoning['session_accepted'] = True
                            break
                await ready()
                metrics['ready_s'] = round(time.monotonic() - start, 3)

                async def send_audio():
                    nonlocal commit_time
                    state = None
                    nonzero = False
                    energy = frames = peak = 0
                    async for chunk in stream:
                        metrics['audio_bytes'] += len(chunk)
                        chunk_peak = audioop.max(chunk, 2)
                        nonzero = nonzero or chunk_peak != 0
                        peak = max(peak, chunk_peak)
                        count = len(chunk) // 2
                        frames += count
                        energy += audioop.rms(chunk, 2) ** 2 * count
                        converted, state = audioop.ratecv(chunk, 2, 1, 16000, 24000, state)
                        if converted:
                            await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(converted).decode('ascii')})
                    metrics.update(audio_duration_ms=round(frames / 16), audio_peak=peak,
                                   audio_rms=round((energy / frames) ** .5, 1) if frames else 0)
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
                        status = response.get('status')
                        metrics['upstream_status'] = status if status in ('completed', 'incomplete', 'failed', 'cancelled') else 'unknown'
                        details = response.get('status_details')
                        reason = details.get('reason') if isinstance(details, dict) else None
                        if reason is not None:
                            metrics['upstream_stop_reason'] = reason if reason in ('max_output_tokens', 'content_filter', 'turn_detected', 'client_cancelled') else 'unknown'
                        usage = response.get('usage')
                        if not isinstance(usage, dict):
                            usage = {}
                        for name in ('input_tokens', 'output_tokens'):
                            value = usage.get(name)
                            if type(value) is int and value >= 0:
                                metrics[name] = value
                        if status != 'completed':
                            raise RealtimeFailure('realtime_incomplete', 'Realtime 未返回完整文字，请重试。', True)
                        text = ''.join(pieces).strip()
                        if not text:
                            raise RealtimeFailure('realtime_no_text', '没有识别到可插入的文字，请检查麦克风和录音后重试。', True)
                        text, metrics['text_normalization'] = normalize_output(text)
                        metrics.update(status='ok', output_chars=len(text),
                            after_commit_s=round(time.monotonic() - commit_time, 3) if commit_time is not None else None,
                            first_after_commit_s=round(first_time - commit_time, 3) if first_time is not None and commit_time is not None else None)
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
            finally:
                if ACTIVE_REASONING is active_reasoning:
                    ACTIVE_REASONING = None


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
