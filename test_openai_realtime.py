"""Synthetic protocol, cancellation, prompt and configuration regressions."""
import asyncio
import audioop
import base64
from datetime import datetime, timezone, timedelta
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import openai_realtime as rt
import realtime_profile as profile
import configure_realtime as configure
from openai_credentials import save_key, load_key, key_status


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metrics = []
        self.logger_patch = patch.object(rt, 'record_metrics', self.metrics.append)
        self.logger_patch.start()
        self.addCleanup(self.logger_patch.stop)
        self.lock = asyncio.Lock()
        self.release = asyncio.Event()
        self.received = []
        self.behavior = 'success'
        self.config = None
        self.upstream_closed = asyncio.Event()
        app = web.Application()
        app.router.add_get('/upstream', self.upstream)
        self.server = TestServer(app)
        await self.server.start_server()
        engine = rt.RealtimeEngine(upstream=str(self.server.make_url('/upstream')),
                     key_loader=lambda: 'synthetic-key', prompt_loader=lambda: ('Synthetic prompt', {'history_turns': 0}))
        local = web.Application()
        async def route(request):
            return await rt.handle(request, self.lock, engine)
        local.router.add_get('/rt', route)
        self.client = TestClient(TestServer(local))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        await self.server.close()

    async def upstream(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            if self.behavior == 'delayed_ready':
                await self.release.wait()
            await ws.send_json({'type': 'session.created', 'session': {'model': rt.MODEL}})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                event = json.loads(message.data)
                kind = event['type']
                if kind == 'session.update':
                    self.config = event['session']
                    await ws.send_json({'type': 'session.updated'})
                elif kind == 'input_audio_buffer.append':
                    self.received.append(base64.b64decode(event['audio']))
                elif kind == 'response.create':
                    await ws.send_json({'type': 'response.output_text.delta', 'delta': '中英 '})
                    if self.behavior == 'upstream_error':
                        await ws.send_json({'type': 'error', 'error': {'code': 'invalid_api_key', 'message': 'do not expose synthetic-key'}})
                        continue
                    await ws.send_json({'type': 'response.output_text.delta', 'delta': 'mixed 完成'})
                    await ws.send_json({'type': 'response.done', 'response': {
                        'status': 'incomplete' if self.behavior == 'incomplete' else 'completed',
                        'usage': {'input_tokens': 10, 'output_tokens': 5}}})
                    if self.behavior == 'hold_close':
                        await self.release.wait()
        finally:
            self.upstream_closed.set()
        return ws

    async def open_stream(self):
        ws = await self.client.ws_connect('/rt')
        await ws.send_json({'type': 'session.update'})
        self.assertEqual((await asyncio.wait_for(ws.receive_json(), 1))['type'], 'session.updated')
        return ws

    async def send(self, ws, chunks=None):
        chunks = chunks if chunks is not None else [b'\x01\x00' * 1600, b'\x02\x00' * 7]
        for chunk in chunks:
            await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(chunk).decode()})
        await ws.send_json({'type': 'session.finish'})
        return chunks

    async def events(self, ws):
        events = []
        while True:
            event = await asyncio.wait_for(ws.receive_json(), 2)
            events.append(event)
            if event['type'] in ('session.finished', 'conversation.item.input_audio_transcription.failed'):
                return events

    async def test_exact_resampling_tail_and_text_only(self):
        ws = await self.open_stream()
        chunks = await self.send(ws)
        events = await self.events(ws)
        expected, _ = audioop.ratecv(b''.join(chunks), 2, 1, 16000, 24000, None)
        self.assertEqual(b''.join(self.received), expected)
        self.assertEqual(events[-2]['transcript'], '中英 mixed 完成')
        self.assertEqual(self.config['output_modalities'], ['text'])
        self.assertIsNone(self.config['audio']['input']['turn_detection'])
        self.assertEqual(self.config['tools'], [])
        await ws.close()
        await asyncio.sleep(.05)
        self.assertFalse(self.lock.locked())

    async def test_buffer_during_connect_and_prompt_ack(self):
        self.behavior = 'delayed_ready'
        ws = await self.open_stream()
        chunks = await self.send(ws)
        self.release.set()
        events = await self.events(ws)
        expected, _ = audioop.ratecv(b''.join(chunks), 2, 1, 16000, 24000, None)
        self.assertEqual(b''.join(self.received), expected)
        self.assertEqual(events[-1]['type'], 'session.finished')

    async def test_final_is_visible_before_upstream_close(self):
        self.behavior = 'hold_close'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-1]['type'], 'session.finished')
        self.assertFalse(self.release.is_set())
        self.release.set()

    async def test_failure_after_partial_never_finishes_successfully(self):
        self.behavior = 'upstream_error'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-1]['type'], 'conversation.item.input_audio_transcription.failed')
        self.assertEqual(events[-1]['error']['code'], 'openai_auth_failed')
        self.assertNotIn('synthetic-key', json.dumps(events))
        self.assertFalse(any(e['type'].endswith('.completed') for e in events))

    async def test_incomplete_response_is_error(self):
        self.behavior = 'incomplete'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-1]['error']['code'], 'realtime_incomplete')

    async def test_silence_does_not_create_a_response(self):
        ws = await self.open_stream()
        await self.send(ws, [bytes(3200)])
        events = await self.events(ws)
        self.assertEqual(events[-1]['error']['code'], 'silent_audio')
        self.assertFalse(any(e['type'].endswith('.completed') for e in events))

    async def test_cancel_during_connect_releases_lock(self):
        self.behavior = 'delayed_ready'
        ws = await self.open_stream()
        await ws.close()
        await asyncio.sleep(.1)
        self.assertFalse(self.lock.locked())
        self.release.set()

    async def test_origin_busy_and_bad_audio(self):
        response = await self.client.get('/rt', headers={'Origin': 'http://example.com'})
        self.assertEqual(response.status, 403)
        ws = await self.open_stream()
        self.assertEqual((await self.client.get('/rt')).status, 429)
        await ws.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(b'x').decode()})
        events = await self.events(ws)
        self.assertEqual(events[-1]['error']['code'], 'bad_audio')


class ProfileAndCredentialTests(unittest.TestCase):
    def test_enable_preserves_llm_shortcuts_and_doubao(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'data'
            state.mkdir()
            initial = {'activeStylePackId': 'builtin.structured', 'defaultMode': 'structured',
                       'shortcut': {'key': 'ControlRight'}, 'selectionShortcut': {'key': 'ShiftRight'}}
            original = {'id': 'builtin.structured', 'kind': 'builtin', 'baseMode': 'structured',
                        'selectionPrompt': 'selection-edit', 'voiceEditPrompt': 'voice-edit', 'prompt': 'style'}
            (root / 'preferences.json').write_text(json.dumps(initial), encoding='utf-8')
            raw_prompt = 'synthetic-default-raw'
            (root / 'style-packs.json').write_text(json.dumps([original, {'id': 'builtin.raw', 'prompt': raw_prompt}]), encoding='utf-8')
            vault = {'active': {'asr': 'doubao-web-stream', 'llm': 'existing-llm'},
                     'providers': {'asr': {'doubao-web-stream': {'model': 'doubao'}}, 'llm': {'existing-llm': {'model': 'synthetic-model'}}}}
            with patch.object(configure, 'STATE', state), patch.object(configure, 'SETTINGS', state / 'settings.json'), \
                 patch.object(configure, 'require_closed'), patch.object(configure, 'load_key', return_value='synthetic'), \
                 patch.object(configure, 'key_status', return_value='configured_unverified'), \
                 patch.object(configure, 'RAW_PROMPT_SHA256', hashlib.sha256(raw_prompt.encode()).hexdigest()), \
                 patch.object(configure, 'read_vault', return_value=({}, vault)), \
                 patch.object(configure, 'write_vault') as written, redirect_stdout(io.StringIO()):
                configure.enable(root)
            result = json.loads((root / 'preferences.json').read_text(encoding='utf-8'))
            self.assertEqual(result['shortcut'], initial['shortcut'])
            self.assertEqual(result['selectionShortcut'], initial['selectionShortcut'])
            self.assertEqual(result['defaultMode'], 'raw')
            enabled_packs = json.loads((root / 'style-packs.json').read_text(encoding='utf-8'))
            direct = next(p for p in enabled_packs if p['id'] == profile.STYLE_ID)
            self.assertEqual(direct['prompt'], raw_prompt, 'custom Raw prompts would call the LLM again')
            self.assertEqual(written.call_args.args[0]['active']['llm'], 'existing-llm')
            self.assertIn('doubao-web-stream', written.call_args.args[0]['providers']['asr'])
            self.assertEqual(written.call_args.args[0]['providers']['llm'], vault['providers']['llm'])

    def test_dpapi_synthetic_key_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'key.dpapi'
            key = 'sk-' + 'x' * 50
            save_key(key, target)
            self.assertNotIn(key.encode(), target.read_bytes())
            self.assertEqual(load_key(target), key)
            self.assertEqual(key_status(target), 'configured_unverified')

    def test_history_is_bounded_and_style_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            def write(name, value):
                (root / name).write_text(json.dumps(value), encoding='utf-8')
            write('preferences.json', {'activeStylePackId': profile.STYLE_ID, 'polishContextWindowMinutes': 40})
            write('style-packs.json', [{'id': profile.STYLE_ID, 'prompt': 'Native prompt'}])
            write('dictionary.json', [{'phrase': 'SyntheticTerm', 'enabled': True}, {'phrase': 'disabled', 'enabled': False}])
            history = [{'createdAt': (now - timedelta(minutes=i+1)).isoformat(), 'stylePackId': profile.STYLE_ID,
                        'finalText': 'turn-' + str(i)} for i in range(4)]
            history.append({'createdAt': now.isoformat(), 'stylePackId': 'other-style', 'finalText': 'unrelated'})
            write('history.json', history)
            with patch.object(profile, 'settings', return_value={'use_history': True}):
                text, meta = profile.prompt_snapshot(root)
            self.assertIn('SyntheticTerm', text)
            self.assertNotIn('disabled', text)
            self.assertIn('turn-0', text)
            self.assertIn('turn-1', text)
            self.assertNotIn('turn-2', text)
            self.assertNotIn('unrelated', text)
            self.assertEqual(meta['history_turns'], 2)

    def test_style_keeps_selection_and_voice_edit(self):
        original = {'id': 'builtin.structured', 'baseMode': 'structured', 'selectionPrompt': 'selection-edit', 'voiceEditPrompt': 'voice-edit'}
        result = configure.make_style(original, 'default-raw-literal')
        self.assertEqual(result['baseMode'], 'raw')
        self.assertEqual(result['kind'], 'imported')
        self.assertEqual(result['selectionPrompt'], 'selection-edit')
        self.assertEqual(result['voiceEditPrompt'], 'voice-edit')
        self.assertEqual(result['prompt'], 'default-raw-literal')
        self.assertEqual(original['baseMode'], 'structured')

    def test_quota_and_generic_errors_do_not_expose_server_text(self):
        error = rt.upstream_failure({'code': 'credit_balance_exhausted', 'message': 'synthetic secret'})
        self.assertEqual(error.details['code'], 'openai_quota_exhausted')
        self.assertNotIn('synthetic secret', json.dumps(error.details))
        self.assertNotIn('private url', json.dumps(rt.public_failure(ValueError('private url'))))


if __name__ == '__main__':
    unittest.main()
