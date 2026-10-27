"""Experimental loopback ASR adapter. No keyboard hooks, microphone or browser.
Protocol derived from the user's local doubao-murmur implementation.
"""
import asyncio
import io
import json
import pathlib
import time
import uuid
import wave
import os
import audioop
import base64
import math
import psutil
import batch_asr
from asr_errors import RecognitionFailure
from credential_store import CREDENTIALS
from login_state import LoginRequired, load_auth, local_login_status
from login_notice import LoginNotifier, MESSAGES, credential_version
from urllib.parse import urlencode
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError, ClientConnectionError

FIXED = dict(version_code='20800', language='zh', device_platform='web', aid='497858',
             real_aid='497858', pkg_type='release_version', pc_version='3.12.3',
             region='', sys_region='', samantha_web='1', format='pcm')
FIXED['use-olympus-account'] = '1'
LOCK = asyncio.Lock()
NOTIFIER = LoginNotifier()
LAST_FAILURE = None
LAST_FAILURE_VERSION = None
LAST_SUCCESS_VERSION = None


class UpstreamRejected(RuntimeError):
    def __init__(self, code):
        value = str(code)
        self.code = value if value.isascii() and value.isdigit() and len(value) <= 12 else 'unknown'
        super().__init__('Upstream rejected request (code only): ' + self.code)


def failure_details(exc):
    global LAST_FAILURE, LAST_FAILURE_VERSION
    if isinstance(exc, RecognitionFailure):
        return exc.details()
    reason = None
    if isinstance(exc, LoginRequired):
        reason = exc.reason
    elif isinstance(exc, UpstreamRejected) and exc.code == '710022013':
        # Observed with expired cookies, but not a documented expiry-only code.
        reason = 'login_check_required'
    elif isinstance(exc, WSServerHandshakeError) and exc.status in (401, 403):
        reason = 'login_rejected'
    if reason in MESSAGES:
        LAST_FAILURE, LAST_FAILURE_VERSION = reason, credential_version()
        NOTIFIER.notify(reason)
        return {'code': reason, 'message': MESSAGES[reason], 'retryable': False, 'recovery': 'relogin_or_check_permissions'}
    if isinstance(exc, UpstreamRejected):
        return {'code': 'upstream_rejected', 'message': str(exc), 'retryable': False, 'recovery': 'check_upstream_service'}
    if isinstance(exc, TimeoutError):
        return RecognitionFailure('recognition_timeout').details()
    if isinstance(exc, (ClientConnectionError, ConnectionError)):
        return {'code': 'network_error', 'message': '豆包连接失败，请检查网络后重试。',
                'retryable': True, 'recovery': 'retry_range_after_network_recovers'}
    # Never return arbitrary exception strings: URLs may contain credentials.
    return {'code': 'asr_failed', 'message': '豆包语音识别失败，请检查网络、录音长度或服务状态。',
            'retryable': False, 'recovery': 'inspect_diagnostics'}


def recognition_succeeded(version):
    global LAST_FAILURE, LAST_FAILURE_VERSION, LAST_SUCCESS_VERSION
    if version == credential_version():
        LAST_FAILURE, LAST_FAILURE_VERSION = None, None
        LAST_SUCCESS_VERSION = version
        NOTIFIER.succeeded()


def login_status():
    status = local_login_status()
    version = credential_version()
    if status['status'] == 'saved_unverified':
        if LAST_FAILURE and LAST_FAILURE_VERSION == version:
            status['status'] = LAST_FAILURE
        elif LAST_SUCCESS_VERSION is not None and LAST_SUCCESS_VERSION == version:
            status.update(status='validated', server_validated=True)
    return status


async def check_login_on_startup(app):
    status = local_login_status()
    NOTIFIER.notify(status['status'])


async def health(request):
    return web.json_response({'status': 'ok', 'keyboard_hooks': False,
                             'independent_session_saved': CREDENTIALS.exists(),
                             'batch_result_contract': 'segments-v1', 'recognition_busy': LOCK.locked(),
                             'login': login_status()})

def split_pcm(pcm):
    """Cut near low-energy audio, retaining every byte exactly once."""
    chunks=[]
    # Keep a short tail with the preceding chunk; a one-second final fragment
    # often produces no server transcript even when preceding chunks succeed.
    while len(pcm)>800000:
        candidates=range(512000,640000,3200)
        cut=min(candidates,key=lambda x:audioop.rms(pcm[x:x+3200],2))+1600
        chunks.append(pcm[:cut]);pcm=pcm[cut:]
    if pcm: chunks.append(pcm)
    return chunks

async def recognize_segments(pcm, audio_offset_seconds=0):
    return await batch_asr.collect_segments(pcm, split_pcm, recognize, failure_details, audio_offset_seconds)

def pcm_from_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getcomptype()) != (16000, 1, 2, 'NONE'):
            raise ValueError('Expected 16 kHz mono PCM16 WAV')
        if w.getnframes() > 16000 * 300:
            raise ValueError('Prototype limit: five minutes')
        return w.readframes(w.getnframes())

async def recognize(pcm, speed=1.0, stream=None, partial=None):
    # Read on each request so independent re-login takes effect without restart.
    version = credential_version()
    auth = load_auth()
    query = dict(FIXED, device_id=auth['device_id'], web_id=auth['web_id'],
                 tea_uuid=auth['web_id'], web_tab_id=str(uuid.uuid4()))
    headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in auth['cookies'].items()),
               'Origin': 'https://www.doubao.com',
               'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'}
    url = 'wss://ws-samantha.doubao.com/samantha/audio/asr?' + urlencode(query)
    started = time.monotonic()
    state = {'text': '', 'messages': 0, 'last': started, 'finished': False, 'error': None,
             'received_messages': 0, 'result_events': 0, 'empty_result_events': 0}
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), trust_env=True) as session:
        async with session.ws_connect(url, headers=headers, heartbeat=20) as ws:
            count = 0
            def diagnostics():
                return {'sent_audio_seconds': count / 32000,
                        'expected_audio_seconds': len(pcm) / 32000 if stream is None else None,
                        'received_messages': state['received_messages'], 'result_events': state['result_events'],
                        'empty_result_events': state['empty_result_events'], 'nonempty_result_events': state['messages'],
                        'upstream_finish': state['finished'], 'upstream_closed': receiver.done(),
                        'upstream_close_code': getattr(ws, 'close_code', None),
                        'elapsed_seconds': round(time.monotonic() - started, 3)}
            async def receive():
                async for msg in ws:
                    if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                        continue
                    try:
                        obj = json.loads(msg.data)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    state['received_messages'] += 1
                    if obj.get('code', 0) != 0:
                        state['error'] = UpstreamRejected(obj.get('code'))
                        return
                    if obj.get('event') == 'result':
                        state['result_events'] += 1
                        text = obj.get('result', {}).get('Text', '')
                        if isinstance(text, str) and text.strip():
                            state.update(text=text, last=time.monotonic())
                            state['messages'] += 1
                            if partial:
                                await partial(text)
                        else:
                            state['empty_result_events'] += 1
                    if obj.get('event') == 'finish':
                        state['finished'] = True
            receiver = asyncio.create_task(receive())
            def check_receiver():
                if state['error']:
                    raise state['error']
                if receiver.done():
                    receiver.result()
                    if not state['finished']:
                        raise RecognitionFailure('upstream_closed', diagnostics())
            try:
                # Drain all real audio BEFORE padding/end wait, unlike an interruptible queue.
                count = 0
                if stream is not None:
                    async for chunk in stream:
                        check_receiver()
                        await ws.send_bytes(chunk)
                        count += len(chunk)
                else:
                    for offset in range(0, len(pcm), 3200):
                        check_receiver()
                        chunk = pcm[offset:offset+3200]
                        await ws.send_bytes(chunk)
                        count += len(chunk)
                        await asyncio.sleep(0.1 / speed)
                    count = len(pcm)
                for _ in range(5):
                    check_receiver()
                    await ws.send_bytes(bytes(3200))
                    await asyncio.sleep(0.1 / speed)
                sent = time.monotonic()
                while time.monotonic() - sent < 10:
                    if state['error']:
                        raise state['error']
                    if receiver.done() and not state['finished']:
                        check_receiver()
                    if state['finished']:
                        break
                    if state['text'] and time.monotonic() - max(sent, state['last']) >= 2:
                        break
                    await asyncio.sleep(0.1)
                if not state['text']:
                    raise RecognitionFailure('no_transcript', diagnostics())
                recognition_succeeded(version)
                return {'text': state['text'], 'audio_seconds': count/32000,
                        'elapsed_seconds': round(time.monotonic()-started, 3),
                        'result_messages': state['messages'],
                        'diagnostics': diagnostics(),
                        'completion': 'finish_event' if state['finished'] else 'quiet_period_heuristic'}
            except Exception:
                # A rejected connection can close while send_bytes is in flight.
                # Preserve the sanitized upstream reason instead of a reset error.
                if state['error']:
                    raise state['error'] from None
                raise
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

async def transcribe(request):
    if request.headers.get('Origin'):
        return web.json_response({'error': 'Browser requests disabled'}, status=403)
    if LOCK.locked():
        return web.json_response({'error': 'One request at a time'}, status=429)
    try:
        reader = await request.multipart()
        audio = None
        audio_offset_seconds = 0.0
        allow_partial = False
        controls = set()
        async for part in reader:
            if part.name == 'file':
                if audio is not None:
                    raise ValueError('Duplicate file')
                data = bytearray()
                while chunk := await part.read_chunk():
                    data.extend(chunk)
                    if len(data) > 10_000_000:
                        raise ValueError('Audio too large')
                audio = bytes(data)
            elif part.name in ('audio_offset_seconds', 'allow_partial'):
                if part.name in controls:
                    raise ValueError('Duplicate control field')
                controls.add(part.name)
                value = bytearray()
                while chunk := await part.read_chunk():
                    value.extend(chunk)
                    if len(value) > 64:
                        raise ValueError('Oversized control field')
                value = value.decode('utf-8').strip()
                if part.name == 'audio_offset_seconds':
                    audio_offset_seconds = float(value)
                    if not math.isfinite(audio_offset_seconds) or not 0 <= audio_offset_seconds <= 1_000_000_000:
                        raise ValueError('Invalid audio offset')
                else:
                    if value.lower() not in ('true', 'false', '1', '0'):
                        raise ValueError('Invalid allow_partial')
                    allow_partial = value.lower() in ('true', '1')
        if audio is None:
            raise ValueError('Missing file')
        pcm = pcm_from_wav(audio)
        async with LOCK:
            result = await recognize_segments(pcm, audio_offset_seconds)
        result['request_id'] = str(uuid.uuid4())
        result['allow_partial'] = allow_partial
        batch_asr.log_incomplete_batch(result)
        return web.json_response(result, status=batch_asr.response_status(result, allow_partial))
    except (ValueError, wave.Error, EOFError):
        return web.json_response({'error': {'code': 'bad_request', 'message': '请检查 WAV 格式、录音长度和请求字段。'}}, status=400)
    except Exception as exc:
        details = failure_details(exc)
        return web.json_response({'error': dict(details, type=type(exc).__name__)}, status=502)

async def realtime(request):
    if request.headers.get('Origin'):
        return web.json_response({'error': 'Browser requests disabled'}, status=403)
    if LOCK.locked():
        return web.json_response({'error': 'One request at a time'}, status=429)
    async with LOCK:
        client = web.WebSocketResponse(max_msg_size=100000, heartbeat=20)
        await client.prepare(request)
        queue = asyncio.Queue(maxsize=100)
        async def audio():
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        async def partial(text):
            await client.send_json({'type': 'conversation.item.input_audio_transcription.text', 'text': text})
        task = None
        count = 0
        try:
            async with asyncio.timeout(330):
                async for message in client:
                    if message.type != WSMsgType.TEXT:
                        break
                    event = json.loads(message.data)
                    kind = event.get('type')
                    if kind == 'session.update' and task is None:
                        task = asyncio.create_task(recognize(b'', stream=audio(), partial=partial))
                        await client.send_json({'type': 'session.updated'})
                    elif kind == 'input_audio_buffer.append' and task is not None:
                        if task.done():
                            await task
                            raise RuntimeError('Stream ended early')
                        chunk = base64.b64decode(event['audio'], validate=True)
                        count += len(chunk)
                        if len(chunk) % 2 or count > 32000 * 300:
                            raise ValueError('Invalid or oversized audio')
                        await asyncio.wait_for(queue.put(chunk), 5)
                    elif kind == 'session.finish' and task is not None:
                        await asyncio.wait_for(queue.put(None), 5)
                        result = await asyncio.wait_for(task, 10)
                        await client.send_json({'type': 'conversation.item.input_audio_transcription.completed',
                                                'transcript': result['text']})
                        await client.send_json({'type': 'session.finished'})
                        break
                    else:
                        raise ValueError('Unexpected event')
        except Exception as exc:
            if not client.closed:
                # failed, not generic error: OpenLess generic error accepts partial text as success.
                await client.send_json({'type': 'conversation.item.input_audio_transcription.failed',
                    'error': failure_details(exc)})
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()
        return client

def main():
    app = web.Application(client_max_size=10_000_000)
    app.on_startup.append(check_login_on_startup)
    owner = os.environ.get('DOUBAO_BRIDGE_OWNER_PID')
    if owner:
        async def lifecycle(application):
            parent = psutil.Process(int(owner))
            async def watch():
                while parent.is_running():
                    await asyncio.sleep(1)
                # The service has no reason to survive a crashed lifecycle owner.
                os._exit(0)
            task = asyncio.create_task(watch())
            yield
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/health', health)
    app.router.add_get('/v1/models', lambda _: web.json_response({'data': [{'id': 'doubao-web-asr', 'object': 'model'}]}))
    app.router.add_post('/v1/audio/transcriptions', transcribe)
    app.router.add_get('/v1/realtime', realtime)
    web.run_app(app, host='127.0.0.1', port=18765, access_log=None, print=None)

if __name__ == '__main__':
    main()
