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
import psutil
from credential_store import CREDENTIALS, load_session
from urllib.parse import urlencode
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

PROFILE = pathlib.Path.home() / 'AppData/Roaming/doubao-murmur/asr_params.json'
FIXED = dict(version_code='20800', language='zh', device_platform='web', aid='497858',
             real_aid='497858', pkg_type='release_version', pc_version='3.12.3',
             region='', sys_region='', samantha_web='1', format='pcm')
FIXED['use-olympus-account'] = '1'
LOCK = asyncio.Lock()

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

async def recognize_segments(pcm):
    chunks=split_pcm(pcm)
    limiter=asyncio.Semaphore(3)
    started=time.monotonic()
    async def one(chunk):
        async with limiter:
            return await recognize(chunk)
    tasks=[asyncio.create_task(one(chunk)) for chunk in chunks]
    try:
        results=await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    return {'text':' '.join(r['text'] for r in results),'audio_seconds':len(pcm)/32000,
            'elapsed_seconds':round(time.monotonic()-started,3),'segments':len(chunks),
            'completion':'ordered_segments_quiet_period_heuristic'}

def pcm_from_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getcomptype()) != (16000, 1, 2, 'NONE'):
            raise ValueError('Expected 16 kHz mono PCM16 WAV')
        if w.getnframes() > 16000 * 300:
            raise ValueError('Prototype limit: five minutes')
        return w.readframes(w.getnframes())

async def recognize(pcm, speed=1.0, stream=None, partial=None):
    # Read on each request so a later Murmur login refresh is picked up.
    if CREDENTIALS.exists():
        auth = load_session()
    elif os.environ.get('DOUBAO_BRIDGE_ALLOW_MURMUR') == '1':
        auth = json.loads(PROFILE.read_text(encoding='utf-8-sig'))
    else:
        raise ValueError('Independent login required')
    if not auth.get('cookies') or not auth.get('device_id') or not auth.get('web_id'):
        raise ValueError('Murmur login data missing')
    query = dict(FIXED, device_id=auth['device_id'], web_id=auth['web_id'],
                 tea_uuid=auth['web_id'], web_tab_id=str(uuid.uuid4()))
    headers = {'Cookie': '; '.join(f'{k}={v}' for k, v in auth['cookies'].items()),
               'Origin': 'https://www.doubao.com',
               'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'}
    url = 'wss://ws-samantha.doubao.com/samantha/audio/asr?' + urlencode(query)
    started = time.monotonic()
    state = {'text': '', 'messages': 0, 'last': started, 'finished': False, 'error': None}
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), trust_env=True) as session:
        async with session.ws_connect(url, headers=headers, heartbeat=20) as ws:
            async def receive():
                async for msg in ws:
                    if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                        continue
                    try:
                        obj = json.loads(msg.data)
                    except (ValueError, UnicodeError):
                        continue
                    if obj.get('code', 0) != 0:
                        state['error'] = 'Upstream rejected request (code only): ' + str(obj.get('code'))
                        return
                    if obj.get('event') == 'result':
                        text = obj.get('result', {}).get('Text', '')
                        if text:
                            state.update(text=text, last=time.monotonic())
                            state['messages'] += 1
                            if partial:
                                await partial(text)
                    if obj.get('event') == 'finish':
                        state['finished'] = True
            receiver = asyncio.create_task(receive())
            try:
                # Drain all real audio BEFORE padding/end wait, unlike an interruptible queue.
                count = 0
                if stream is not None:
                    async for chunk in stream:
                        if state['error'] or receiver.done():
                            raise RuntimeError('Upstream stream closed')
                        await ws.send_bytes(chunk)
                        count += len(chunk)
                else:
                    for offset in range(0, len(pcm), 3200):
                        if state['error']:
                            raise RuntimeError(state['error'])
                        await ws.send_bytes(pcm[offset:offset+3200])
                        await asyncio.sleep(0.1 / speed)
                    count = len(pcm)
                for _ in range(5):
                    await ws.send_bytes(bytes(3200))
                    await asyncio.sleep(0.1 / speed)
                sent = time.monotonic()
                while time.monotonic() - sent < 10:
                    if state['error']:
                        raise RuntimeError(state['error'])
                    if state['finished']:
                        break
                    if state['text'] and time.monotonic() - max(sent, state['last']) >= 2:
                        break
                    await asyncio.sleep(0.1)
                if not state['text']:
                    raise RuntimeError('No transcript received')
                return {'text': state['text'], 'audio_seconds': count/32000,
                        'elapsed_seconds': round(time.monotonic()-started, 3),
                        'result_messages': state['messages'],
                        'completion': 'finish_event' if state['finished'] else 'quiet_period_heuristic'}
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
        async for part in reader:
            if part.name == 'file':
                data = bytearray()
                while chunk := await part.read_chunk():
                    data.extend(chunk)
                    if len(data) > 10_000_000:
                        raise ValueError('Audio too large')
                audio = bytes(data)
        if audio is None:
            raise ValueError('Missing file')
        pcm = pcm_from_wav(audio)
        async with LOCK:
            result = await asyncio.wait_for(recognize_segments(pcm), len(pcm)/32000 + 40)
        return web.json_response(result)
    except Exception as exc:
        # Never return exception strings: network exceptions can include credential URLs.
        reason=str(exc) if isinstance(exc,RuntimeError) and (str(exc).startswith('Upstream rejected request (code only):') or str(exc)=='No transcript received') else 'ASR failed; check login, audio or connectivity'
        return web.json_response({'error': {'message': reason,
                                           'type': type(exc).__name__}}, status=502)

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
        except Exception:
            if not client.closed:
                # failed, not generic error: OpenLess generic error accepts partial text as success.
                await client.send_json({'type': 'conversation.item.input_audio_transcription.failed',
                    'error': {'message': 'Doubao stream failed; check login, connectivity or recording length'}})
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()
        return client

def main():
    app = web.Application(client_max_size=10_000_000)
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
    app.router.add_get('/health', lambda _: web.json_response({'status': 'ok', 'keyboard_hooks': False,
                         'independent_session_saved': CREDENTIALS.exists()}))
    app.router.add_get('/v1/models', lambda _: web.json_response({'data': [{'id': 'doubao-web-asr', 'object': 'model'}]}))
    app.router.add_post('/v1/audio/transcriptions', transcribe)
    app.router.add_get('/v1/realtime', realtime)
    web.run_app(app, host='127.0.0.1', port=18765, access_log=None, print=None)

if __name__ == '__main__':
    main()
