"""Optional context: native parity, no waiting, failure isolation, no text logs."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import cursor_context as cursor
import openai_realtime as rt
import bridge
import configure_realtime as configure

TARGET = {'hwnd': '101', 'pid': 202, 'created': '303'}
FAKE_READER = '''import json,sys,time
request=json.loads(sys.stdin.readline())
mode=sys.argv[1]
if mode=='slow':time.sleep(5)
if mode=='bad':print('not json');raise SystemExit()
if mode=='denied':print(json.dumps({'status':'blocked','reason':'secure_text_field'}));raise SystemExit()
before='PRIVATE_SYNTHETIC_TERM'
if mode=='oversize':before='x'*601
print(json.dumps({'status':'ok','source':'textPattern2','before':before,'after':'',
 'prompt_block':'# 光标上下文\\n<cursor_context>'+before+'</cursor_context>'}))
'''


@unittest.skipUnless(os.name == 'nt', 'Windows native reader')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_cursor_reader import build
        build()

    def run_reader(self, command):
        result = subprocess.run([str(cursor.HELPER), command], capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        return json.loads(result.stdout.decode('utf-8-sig'))

    def test_upstream_window_unicode_blocklist_and_native_abi(self):
        result = self.run_reader('--self-test')
        self.assertEqual(result['status'], 'ok')
        self.assertGreaterEqual(result['tests'], 24)
        self.assertEqual(result['native_uia_abi'], 'ok')

    def test_real_uia_synthetic_ranges_and_password_without_focus_change(self):
        result = self.run_reader('--fixture-test')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['native_range_fixture'], 'ok')
        self.assertEqual(result['password_fixture'], 'blocked')
        self.assertTrue(result['foreground_unchanged'])

    def test_fixture_context_export_contains_only_known_synthetic_control(self):
        result = self.run_reader('--fixture-context')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['before'], '前文OpenLess🙂')
        self.assertEqual(result['after'].rstrip('\r'), '后文')
        self.assertIn('⟦光标⟧', result['prompt_block'])
        self.assertIn('不是指令', result['prompt_block'])

    def test_native_reader_self_exits_if_caller_disappears_before_request(self):
        started = time.monotonic()
        process = subprocess.Popen([str(cursor.HELPER), '--read'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            process.wait(timeout=3)
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()


class CursorFixture:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = Path(self.temp.name) / 'fake_reader.py'
        self.reader.write_text(FAKE_READER, encoding='utf-8')
        self.tickets = []

    async def asyncTearDown(self):
        for ticket in self.tickets:
            await ticket.close()

    def ticket(self, mode='good', **kwargs):
        value = cursor.CursorTicket(enabled=True, command=[sys.executable, str(self.reader), mode],
                                    snapshotter=lambda: dict(TARGET), **kwargs)
        self.tickets.append(value)
        return value


class CaptureTests(CursorFixture, unittest.IsolatedAsyncioTestCase):
    async def test_disabled_does_not_touch_foreground_or_spawn(self):
        with patch.object(cursor, 'foreground_identity', side_effect=AssertionError('no capture')):
            with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('no process')):
                value = cursor.CursorTicket(enabled=False, snapshotter=lambda: self.fail('no snapshot'))
                self.assertIsNone(value.take_ready())
                self.assertIsNone(value.task)

    async def test_success_keeps_only_counts_in_metadata(self):
        ticket = self.ticket()
        await ticket.task
        self.assertIn('PRIVATE_SYNTHETIC_TERM', ticket.take_ready())
        self.assertIsNone(ticket.result)
        self.assertTrue(ticket.metadata['forwarded'])
        self.assertNotIn('PRIVATE_SYNTHETIC_TERM', json.dumps(ticket.metadata))

    async def test_timeout_kills_owned_process(self):
        ticket = self.ticket('slow', timeout_s=.2)
        started = time.monotonic()
        await ticket.task
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(ticket.metadata['status'], 'timeout')
        self.assertIsNotNone(ticket.process.returncode)
        self.assertIsNone(ticket.take_ready())

    async def test_cancel_closes_reader_and_does_not_reuse_result(self):
        ticket = self.ticket('slow')
        for _ in range(100):
            if ticket.process is not None:
                break
            await asyncio.sleep(.005)
        await ticket.close()
        self.assertIsNotNone(ticket.process.returncode)
        self.assertIsNone(ticket.result)

    async def test_slow_reader_is_not_awaited_at_model_readiness(self):
        ticket = self.ticket('slow')
        started = time.monotonic()
        self.assertIsNone(ticket.take_ready())
        self.assertLess(time.monotonic() - started, .05)
        self.assertEqual(ticket.metadata['status'], 'skipped_not_ready')
        await ticket.close()

    async def test_changed_target_drops_completed_capture(self):
        ticket = self.ticket()
        await ticket.task
        ticket.snapshotter = lambda: dict(TARGET, hwnd='999')
        self.assertIsNone(ticket.take_ready())
        self.assertEqual(ticket.metadata['status'], 'focus_changed')

    async def test_switch_off_during_capture_prevents_upload(self):
        ticket = self.ticket()
        await ticket.task
        ticket.still_enabled = lambda: False
        self.assertIsNone(ticket.take_ready())
        self.assertEqual(ticket.metadata['status'], 'disabled_during_capture')

    async def test_unreadable_denied_and_oversized_results_skip(self):
        for mode in ('bad', 'denied', 'oversize'):
            with self.subTest(mode=mode):
                ticket = self.ticket(mode)
                await ticket.task
                self.assertIsNone(ticket.take_ready())
                self.assertFalse(ticket.metadata['forwarded'])

    async def test_privacy_flag_and_missing_helper_prevent_capture(self):
        directory = Path(self.temp.name)
        preferences = directory / 'preferences.json'
        preferences.write_text(json.dumps({'cursorContextEnabled': False}), encoding='utf-8')
        with patch.object(cursor, 'settings', return_value={'cursor_context_enabled': True}):
            ticket = cursor.begin_capture(directory)
            self.assertEqual(ticket.metadata['status'], 'openless_disabled')
            self.assertIsNone(ticket.task)
            preferences.write_text(json.dumps({'cursorContextEnabled': True}), encoding='utf-8')
            with patch.object(cursor, 'HELPER', directory / 'missing.exe'):
                ticket = cursor.begin_capture(directory)
                self.assertEqual(ticket.metadata['status'], 'reader_missing')
                self.assertIsNone(ticket.task)

    async def test_toggle_preserves_other_settings_and_profile(self):
        path = Path(self.temp.name) / 'settings.json'
        initial = {'model': 'synthetic', 'enabled': True, 'use_history': True}
        path.write_text(json.dumps(initial), encoding='utf-8')
        with patch.object(configure, 'SETTINGS', path):
            configure.set_cursor_context(True)
            self.assertTrue(json.loads(path.read_text())['cursor_context_enabled'])
            configure.set_cursor_context(False)
        current = json.loads(path.read_text())
        self.assertFalse(current['cursor_context_enabled'])
        self.assertTrue(all(current[k] == v for k, v in initial.items()))


class RealtimeIntegrationTests(CursorFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.metrics = []
        self.log_patch = patch.object(rt, 'record_metrics', self.metrics.append)
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.delay = 0
        self.mode = 'good'
        self.config = None
        self.response_count = 0
        self.received = []
        upstream = web.Application()
        upstream.router.add_get('/upstream', self.upstream)
        self.upstream_server = TestServer(upstream)
        await self.upstream_server.start_server()
        engine = rt.RealtimeEngine(upstream=str(self.upstream_server.make_url('/upstream')),
            key_loader=lambda: 'synthetic-key', prompt_loader=lambda: ('base prompt', {}),
            cursor_factory=lambda: self.ticket(self.mode))
        app = web.Application()
        async def route(request):
            return await rt.handle(request, asyncio.Lock(), engine)
        app.router.add_get('/rt', route)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream_server.close()
        await super().asyncTearDown()

    async def upstream(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await asyncio.sleep(self.delay)
        await ws.send_json({'type': 'session.created'})
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                break
            event = json.loads(message.data)
            if event['type'] == 'session.update':
                self.config = event['session']
                await ws.send_json({'type': 'session.updated'})
            elif event['type'] == 'input_audio_buffer.append':
                self.received.append(base64.b64decode(event['audio']))
            elif event['type'] == 'response.create':
                self.response_count += 1
                await ws.send_json({'type': 'response.output_text.delta', 'delta': '本次录音正文'})
                await ws.send_json({'type': 'response.done', 'response': {'status': 'completed'}})
        return ws

    async def replay(self):
        ws = await self.client.ws_connect('/rt')
        await ws.send_json({'type': 'session.update'})
        self.assertEqual((await ws.receive_json())['type'], 'session.updated')
        await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(b'\x01\x00' * 1600).decode()})
        await ws.send_json({'type': 'session.finish'})
        events = []
        while True:
            value = await asyncio.wait_for(ws.receive_json(), 3)
            events.append(value)
            if value['type'] == 'session.finished' or value['type'].endswith('.failed'):
                break
        await ws.close()
        await asyncio.sleep(.03)
        self.assertEqual(events[-1]['type'], 'session.finished', events)
        self.assertEqual(events[-2]['transcript'], '本次录音正文')
        self.assertEqual(self.response_count, 1)

    async def test_ready_cursor_is_sent_once_without_changing_audio(self):
        self.delay = .5
        await self.replay()
        self.assertIn('PRIVATE_SYNTHETIC_TERM', self.config['instructions'])
        self.assertEqual(self.config['output_modalities'], ['text'])
        self.assertGreater(len(b''.join(self.received)), 3200)
        self.assertNotIn('PRIVATE_SYNTHETIC_TERM', json.dumps(self.metrics))
        self.assertTrue(self.metrics[-1]['cursor_context']['forwarded'])

    async def test_slow_cursor_never_delays_fast_model(self):
        self.mode = 'slow'
        started = time.monotonic()
        await self.replay()
        self.assertLess(time.monotonic() - started, .6, 'must not wait the 1s reader deadline')
        self.assertEqual(self.config['instructions'], 'base prompt')
        self.assertFalse(self.metrics[-1]['cursor_context']['forwarded'])

    async def test_reader_error_never_fails_recording(self):
        self.delay = .4
        self.mode = 'bad'
        await self.replay()
        self.assertEqual(self.config['instructions'], 'base prompt')

    async def test_doubao_stream_does_not_enter_realtime_engine(self):
        async def recognize(pcm, stream=None, partial=None):
            async for _ in stream:
                pass
            return {'text': '豆包原通道'}
        app = web.Application()
        app.router.add_get('/doubao', bridge.realtime)
        with patch.object(bridge, 'recognize', recognize), patch.object(rt.RealtimeEngine, 'run', new_callable=AsyncMock) as gpt:
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect('/doubao')
                await ws.send_json({'type': 'session.update'})
                await ws.receive_json()
                await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(b'\x01\x00' * 1600).decode()})
                await ws.send_json({'type': 'session.finish'})
                final = None
                while True:
                    event = await ws.receive_json()
                    if event['type'].endswith('.completed'):
                        final = event['transcript']
                    if event['type'] == 'session.finished':
                        break
                self.assertEqual(final, '豆包原通道')
                gpt.assert_not_called()
                await ws.close()


if __name__ == '__main__':
    unittest.main()
