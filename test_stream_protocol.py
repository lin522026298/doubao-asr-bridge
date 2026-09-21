"""Local protocol checks without credentials or cloud traffic."""
import asyncio, base64
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import bridge

async def main():
    received=[]
    cleaned=asyncio.Event()
    async def fake(pcm, stream=None, partial=None):
        try:
            async for chunk in stream:
                received.append(chunk)
                await partial('中英 mixed')
            return {'text':'中英 mixed 完成'}
        finally:
            cleaned.set()
    bridge.recognize=fake
    app=web.Application()
    app.router.add_get('/v1/realtime',bridge.realtime)
    async with TestClient(TestServer(app)) as client:
        r=await client.get('/v1/realtime',headers={'Origin':'http://example.com'})
        assert r.status==403
        ws=await client.ws_connect('/v1/realtime')
        await ws.send_json({'type':'session.update'})
        assert (await ws.receive_json())['type']=='session.updated'
        assert (await client.get('/v1/realtime')).status==429
        chunks=[b'\x01\x00'*1600,b'\x02\x00'*7]
        for chunk in chunks:
            await ws.send_json({'type':'input_audio_buffer.append','audio':base64.b64encode(chunk).decode()})
        await ws.send_json({'type':'session.finish'})
        events=[]
        while True:
            obj=await ws.receive_json();events.append(obj)
            if obj['type']=='session.finished':break
        assert received==chunks, 'final short audio frame lost or reordered'
        assert events[-2]['transcript']=='中英 mixed 完成'
        await ws.close()
        await asyncio.sleep(.05)
        assert not bridge.LOCK.locked()
        cleaned.clear()
        ws=await client.ws_connect('/v1/realtime')
        await ws.send_json({'type':'session.update'})
        await ws.receive_json()
        await ws.close()
        await asyncio.wait_for(cleaned.wait(),1)
        await asyncio.sleep(.05)
        assert not bridge.LOCK.locked(), 'cancel leaked lock'
    print('PASS: interim/final ordering, exact tail bytes, browser rejection, busy rejection, cancellation cleanup')

asyncio.run(main())
