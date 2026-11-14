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
        self.configs = []
        self.session_errors = []
        self.upstream_events = []
        self.response_requested = asyncio.Event()
        self.retry_received = asyncio.Event()
        self.settings_value = {}
        self.settings_error = None
        self.settings_calls = 0
        self.settings_loaded = asyncio.Event()
        self.upstream_closed = asyncio.Event()
        app = web.Application()
        app.router.add_get('/upstream', self.upstream)
        self.server = TestServer(app)
        await self.server.start_server()
        def settings_loader():
            self.settings_calls += 1
            self.settings_loaded.set()
            if self.settings_error is not None:
                raise self.settings_error
            return self.settings_value
        engine = rt.RealtimeEngine(upstream=str(self.server.make_url('/upstream')),
                     key_loader=lambda: 'synthetic-key', prompt_loader=lambda: ('Synthetic prompt', {'history_turns': 0}),
                     cursor_factory=lambda: None, settings_loader=settings_loader)
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
                self.upstream_events.append(kind)
                if kind == 'session.update':
                    self.config = event['session']
                    self.configs.append(self.config)
                    if self.behavior == 'close_before_session_ack':
                        await ws.close()
                        break
                    if self.session_errors:
                        error = self.session_errors.pop(0)
                        if error is not None:
                            await ws.send_json({'type': 'error', 'error': error})
                            continue
                    if self.behavior == 'hold_retry_ack' and len(self.configs) > 1:
                        self.retry_received.set()
                        await self.release.wait()
                        if request.transport is None or request.transport.is_closing():
                            break
                    await ws.send_json({'type': 'session.updated'})
                elif kind == 'input_audio_buffer.append':
                    self.received.append(base64.b64decode(event['audio']))
                elif kind == 'response.create':
                    self.response_requested.set()
                    if self.behavior == 'hold_response':
                        await self.release.wait()
                    if self.behavior == 'empty_completed':
                        await ws.send_json({'type': 'response.done', 'response': {'status': 'completed',
                            'usage': {'input_tokens': 10, 'output_tokens': 0}}})
                        continue
                    await ws.send_json({'type': 'response.output_text.delta', 'delta': '這是 ' if self.behavior == 'traditional' else '中英 '})
                    if self.behavior == 'upstream_error':
                        await ws.send_json({'type': 'error', 'error': {'code': 'invalid_api_key', 'message': 'do not expose synthetic-key'}})
                        continue
                    await ws.send_json({'type': 'response.output_text.delta', 'delta': 'API，目的是什麼？' if self.behavior == 'traditional' else 'mixed 完成'})
                    await ws.send_json({'type': 'response.done', 'response': {
                        'status': 'incomplete' if self.behavior == 'incomplete' else 'completed',
                        'status_details': {'reason': 'max_output_tokens'} if self.behavior == 'incomplete' else None,
                        'usage': None if self.behavior == 'null_usage' else {'input_tokens': 10, 'output_tokens': 5}}})
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

    async def close_and_wait(self, ws, metric_count):
        await ws.close()
        async with asyncio.timeout(1):
            while self.lock.locked() or len(self.metrics) < metric_count:
                await asyncio.sleep(.01)

    def reasoning_health(self):
        def settings_loader():
            if self.settings_error is not None:
                raise self.settings_error
            return self.settings_value
        with patch.object(rt, 'key_status', return_value='synthetic'), \
             patch.object(rt, 'cursor_policy', return_value={'enabled': False}):
            return rt.health_status(settings_loader=settings_loader)

    async def test_default_reasoning_is_sent_and_reported(self):
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.config['reasoning'], {'effort': 'low'})
        self.assertEqual(self.metrics[-1]['reasoning'], 'low')
        self.assertEqual(self.metrics[-1]['reasoning_config']['status'], 'default')
        self.assertEqual(self.settings_calls, 1)
        self.assertEqual(self.reasoning_health()['reasoning'], 'low')
        self.assertIsNone(self.reasoning_health()['active_reasoning'])

    async def test_each_supported_reasoning_effort_reaches_session_update(self):
        for index, effort in enumerate(('minimal', 'low', 'medium', 'high', 'xhigh'), 1):
            with self.subTest(effort=effort):
                self.settings_value = {'reasoning': effort}
                ws = await self.open_stream()
                await self.send(ws)
                self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
                await self.close_and_wait(ws, index)
                self.assertEqual(self.config['reasoning'], {'effort': effort})
                self.assertEqual(self.metrics[-1]['reasoning'], effort)
                self.assertEqual(self.metrics[-1]['reasoning_config']['status'], 'configured')
                self.assertEqual(self.reasoning_health()['reasoning'], effort)
        self.assertEqual(self.settings_calls, 5)

    async def test_none_is_opt_in_experimental_in_session_health_and_metrics(self):
        self.settings_value = {'reasoning': 'none'}
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.config['reasoning'], {'effort': 'none'})
        self.assertEqual(self.metrics[-1]['reasoning'], 'none')
        self.assertEqual(self.metrics[-1]['reasoning_config']['status'], 'configured_experimental')
        health = self.reasoning_health()
        self.assertEqual(health['reasoning'], 'none')
        self.assertEqual(health['reasoning_config']['status'], 'configured_experimental')
        self.assertEqual(self.settings_calls, 1)

    async def test_none_schema_rejection_retries_low_once_before_any_audio(self):
        self.settings_value = {'reasoning': 'none'}
        self.session_errors = [{'code': 'invalid_value', 'type': 'invalid_request_error',
            'param': 'session.reasoning.effort', 'message': 'synthetic-key private error text'}]
        self.behavior = 'hold_response'
        ws = await self.open_stream()
        chunks = await self.send(ws)
        await asyncio.wait_for(self.response_requested.wait(), 1)
        health = self.reasoning_health()
        self.assertEqual(health['reasoning'], 'low')
        self.assertEqual(health['active_reasoning']['configured_effort'], 'none')
        self.assertEqual(health['active_reasoning']['status'], 'compatibility_fallback')
        self.assertTrue(health['active_reasoning']['session_accepted'])
        self.assertEqual(health['reasoning_config']['effort'], 'none')
        self.release.set()
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual([c['reasoning']['effort'] for c in self.configs], ['none', 'low'])
        self.assertEqual(self.upstream_events[:2], ['session.update', 'session.update'])
        self.assertEqual(self.upstream_events.count('input_audio_buffer.commit'), 1)
        self.assertEqual(self.upstream_events.count('response.create'), 1)
        expected, _ = audioop.ratecv(b''.join(chunks), 2, 1, 16000, 24000, None)
        self.assertEqual(b''.join(self.received), expected, 'The retry must preserve every audio frame and tail')
        metric = self.metrics[-1]
        self.assertEqual(metric['reasoning_config']['effort'], 'none')
        self.assertEqual(metric['reasoning'], 'low')
        self.assertEqual(metric['reasoning_sent'], ['none', 'low'])
        self.assertEqual(metric['reasoning_session_accepted'], 'low')
        self.assertEqual(metric['reasoning_fallback'], {
            'from': 'none', 'to': 'low', 'reason': 'schema_effort_rejected',
            'error_code': 'invalid_value', 'error_param': 'session.reasoning.effort'})
        self.assertNotIn('synthetic-key', json.dumps([metric, health]))
        self.assertIsNone(self.reasoning_health()['active_reasoning'])

    async def test_none_fallback_accepts_only_the_enumerated_schema_codes_and_paths(self):
        cases = (('unsupported_value', 'reasoning.effort'),
                 ('invalid_parameter', 'session.reasoning.effort'),
                 ('invalid_enum_value', 'reasoning.effort'),
                 ('unsupported_parameter_value', 'session.reasoning.effort'))
        for index, (code, param) in enumerate(cases, 1):
            with self.subTest(code=code, param=param):
                self.settings_value = {'reasoning': 'none'}
                self.session_errors = [{'code': code, 'param': param}]
                self.configs.clear()
                ws = await self.open_stream()
                await self.send(ws)
                self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
                await self.close_and_wait(ws, index)
                self.assertEqual([c['reasoning']['effort'] for c in self.configs], ['none', 'low'])
                self.assertEqual(self.metrics[-1]['reasoning_session_accepted'], 'low')

    async def test_unrelated_errors_other_efforts_and_disabled_policy_never_retry(self):
        cases = [
            ({'reasoning': 'none'}, {'code': 'invalid_api_key', 'param': 'session.reasoning.effort'}),
            ({'reasoning': 'none'}, {'code': 'insufficient_quota', 'param': 'session.reasoning.effort'}),
            ({'reasoning': 'none'}, {'code': 'rate_limit_exceeded', 'param': 'session.reasoning.effort'}),
            ({'reasoning': 'none'}, {'code': 'invalid_value', 'param': 'session.audio.input.format'}),
            ({'reasoning': 'none'}, {'code': 'invalid_value', 'param': 'session.reasoning'}),
            ({'reasoning': 'none'}, {'code': 'invalid_value', 'param': 'response.reasoning.effort'}),
            ({'reasoning': 'none'}, {'code': 'invalid_value'}),
            ({'reasoning': 'none'}, {'code': 'unknown_error', 'param': 'reasoning.effort'}),
            ({'reasoning': 'none'}, {'code': 'invalid_value', 'param': 'reasoning.effort', 'type': 'authentication_error'}),
            ({'reasoning': 'medium'}, {'code': 'invalid_value', 'param': 'reasoning.effort'}),
            ({'reasoning': 'none', 'reasoning_compat_fallback': False}, {'code': 'invalid_value', 'param': 'reasoning.effort'}),
        ]
        for index, (config, error) in enumerate(cases, 1):
            with self.subTest(case=index):
                self.settings_value = config
                self.session_errors = [dict(error, message='synthetic-key says session.reasoning.effort is invalid')]
                self.configs.clear()
                self.upstream_events.clear()
                ws = await self.open_stream()
                await self.send(ws)
                self.assertEqual((await self.events(ws))[-1]['type'], 'conversation.item.input_audio_transcription.failed')
                await self.close_and_wait(ws, index)
                self.assertEqual([c['reasoning']['effort'] for c in self.configs], [config['reasoning']])
                self.assertEqual(self.upstream_events, ['session.update'])
                self.assertIsNone(self.metrics[-1]['reasoning_fallback'])
                self.assertIsNone(self.metrics[-1]['reasoning_session_accepted'])
                self.assertNotIn('synthetic-key', json.dumps(self.metrics[-1]))
                self.assertIsNone(self.reasoning_health()['active_reasoning'])

    async def test_second_effort_rejection_is_not_retried_again(self):
        self.settings_value = {'reasoning': 'none'}
        error = {'code': 'invalid_value', 'param': 'session.reasoning.effort'}
        self.session_errors = [error, error]
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'conversation.item.input_audio_transcription.failed')
        await self.close_and_wait(ws, 1)
        self.assertEqual([c['reasoning']['effort'] for c in self.configs], ['none', 'low'])
        self.assertEqual(self.upstream_events, ['session.update', 'session.update'])
        self.assertEqual(self.metrics[-1]['reasoning_sent'], ['none', 'low'])
        self.assertIsNone(self.metrics[-1]['reasoning_session_accepted'])

    async def test_network_close_before_ack_never_retries(self):
        self.settings_value = {'reasoning': 'none'}
        self.behavior = 'close_before_session_ack'
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'conversation.item.input_audio_transcription.failed')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.upstream_events, ['session.update'])
        self.assertEqual(self.metrics[-1]['reasoning_sent'], ['none'])
        self.assertIsNone(self.metrics[-1]['reasoning_fallback'])

    async def test_cancel_while_waiting_for_retry_ack_clears_active_state(self):
        self.settings_value = {'reasoning': 'none'}
        self.session_errors = [{'code': 'invalid_value', 'param': 'session.reasoning.effort'}]
        self.behavior = 'hold_retry_ack'
        ws = await self.open_stream()
        await asyncio.wait_for(self.retry_received.wait(), 1)
        self.assertEqual(self.reasoning_health()['reasoning'], 'low')
        self.assertFalse(self.reasoning_health()['active_reasoning']['session_accepted'])
        await self.close_and_wait(ws, 1)
        self.release.set()
        self.assertEqual(self.upstream_events, ['session.update', 'session.update'])
        self.assertEqual(self.metrics[-1]['status'], 'cancelled')
        self.assertIsNone(self.metrics[-1]['reasoning_session_accepted'])
        self.assertIsNone(self.reasoning_health()['active_reasoning'])

    async def test_compatibility_policy_is_frozen_with_the_recording(self):
        self.settings_value = {'reasoning': 'none', 'reasoning_compat_fallback': True}
        self.session_errors = [{'code': 'invalid_value', 'param': 'reasoning.effort'}]
        self.behavior = 'delayed_ready'
        ws = await self.open_stream()
        await asyncio.wait_for(self.settings_loaded.wait(), 1)
        self.settings_value['reasoning_compat_fallback'] = False
        await self.send(ws)
        self.release.set()
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.metrics[-1]['reasoning_sent'], ['none', 'low'])
        self.assertTrue(self.metrics[-1]['reasoning_config']['compat_fallback_enabled'])
        self.assertFalse(self.reasoning_health()['reasoning_config']['compat_fallback_enabled'])
        self.assertEqual(self.settings_calls, 1)

    async def test_reasoning_freezes_before_connect_and_reloads_next_recording(self):
        self.behavior = 'delayed_ready'
        self.settings_value = {'reasoning': 'medium'}
        ws = await self.open_stream()
        await asyncio.wait_for(self.settings_loaded.wait(), 1)
        self.settings_value['reasoning'] = 'high'
        active = self.reasoning_health()
        self.assertEqual(active['reasoning'], 'medium')
        self.assertEqual(active['reasoning_source'], 'active_recording')
        self.assertEqual(active['active_reasoning']['effort'], 'medium')
        self.assertEqual(active['reasoning_config']['effort'], 'high')
        await self.send(ws)
        self.release.set()
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.configs[0]['reasoning'], {'effort': 'medium'})
        self.assertEqual(self.metrics[0]['reasoning'], 'medium')
        self.assertEqual(self.settings_calls, 1)
        self.assertEqual(self.reasoning_health()['reasoning_source'], 'next_recording')
        self.assertIsNone(self.reasoning_health()['active_reasoning'])

        self.behavior = 'success'
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 2)
        self.assertEqual(self.configs[1]['reasoning'], {'effort': 'high'})
        self.assertEqual(self.metrics[1]['reasoning'], 'high')
        self.assertEqual(self.settings_calls, 2)

    async def test_invalid_reasoning_falls_back_without_logging_raw_settings(self):
        values = ('disabled', None, False, 42, ['synthetic-key'], {'private': 'synthetic-key'}, 'synthetic-key')
        for index, value in enumerate(values, 1):
            with self.subTest(value_type=type(value).__name__):
                self.settings_value = {'reasoning': value, 'private': 'synthetic-key'}
                ws = await self.open_stream()
                await self.send(ws)
                self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
                await self.close_and_wait(ws, index)
                self.assertEqual(self.config['reasoning'], {'effort': 'low'})
                self.assertEqual(self.metrics[-1]['reasoning'], 'low')
                self.assertEqual(self.metrics[-1]['reasoning_config']['status'], 'invalid_fallback')
                health = self.reasoning_health()
                self.assertEqual(health['reasoning'], 'low')
                self.assertEqual(health['reasoning_config']['status'], 'invalid_fallback')
                self.assertNotIn('synthetic-key', json.dumps([self.metrics[-1], health]))

    async def test_unavailable_settings_fall_back_and_keep_safe_diagnostics(self):
        self.settings_error = OSError('synthetic-key and private settings details')
        ws = await self.open_stream()
        await self.send(ws)
        self.assertEqual((await self.events(ws))[-1]['type'], 'session.finished')
        await self.close_and_wait(ws, 1)
        self.assertEqual(self.config['reasoning'], {'effort': 'low'})
        self.assertEqual(self.metrics[-1]['reasoning_config'], {
            'effort': 'low', 'status': 'unavailable_fallback', 'reason': 'settings_read_failed'})
        health = self.reasoning_health()
        self.assertEqual(health['reasoning_config']['status'], 'unavailable_fallback')
        self.assertNotIn('synthetic-key', json.dumps([self.metrics, health]))
        self.assertEqual(self.settings_calls, 1)

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
        await ws.close()
        await asyncio.sleep(.05)
        self.assertEqual(self.metrics[-1]['upstream_status'], 'incomplete')
        self.assertEqual(self.metrics[-1]['upstream_stop_reason'], 'max_output_tokens')

    async def test_completed_but_empty_is_distinct_from_truncation(self):
        self.behavior = 'empty_completed'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-1]['error']['code'], 'realtime_no_text')
        self.assertFalse(any(e['type'].endswith('.completed') for e in events))
        await ws.close()
        await asyncio.sleep(.05)
        self.assertEqual(self.metrics[-1]['upstream_status'], 'completed')
        self.assertEqual(self.metrics[-1]['output_tokens'], 0)

    async def test_quiet_nonzero_audio_is_not_filtered(self):
        ws = await self.open_stream()
        await self.send(ws, [b'\x01\x00' * 1600])
        events = await self.events(ws)
        self.assertEqual(events[-1]['type'], 'session.finished')
        await ws.close()
        await asyncio.sleep(.05)
        self.assertEqual(self.metrics[-1]['audio_rms'], 1)
        self.assertEqual(self.metrics[-1]['audio_peak'], 1)
        self.assertEqual(self.metrics[-1]['audio_duration_ms'], 100)

    async def test_only_final_text_is_normalized_without_another_response(self):
        self.behavior = 'traditional'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-2]['transcript'], '这是 API，目的是什么？')
        self.assertEqual(sum(e['type'].endswith('.completed') for e in events), 1)
        await ws.close()
        await asyncio.sleep(.05)
        self.assertTrue(self.metrics[-1]['text_normalization']['changed'])

    async def test_optional_usage_diagnostics_do_not_break_valid_text(self):
        self.behavior = 'null_usage'
        ws = await self.open_stream()
        await self.send(ws)
        events = await self.events(ws)
        self.assertEqual(events[-1]['type'], 'session.finished')
        self.assertEqual(events[-2]['transcript'], '中英 mixed 完成')

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
    def test_reasoning_file_defaults_and_corruption_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with patch.object(rt, 'REALTIME_SETTINGS', path):
                self.assertEqual(rt.reasoning_snapshot(), {'effort': 'low', 'status': 'default'})
                path.write_text(json.dumps({'reasoning': 'minimal'}), encoding='utf-8-sig')
                self.assertEqual(rt.reasoning_snapshot(), {'effort': 'minimal', 'status': 'configured'})
                path.write_text('{invalid synthetic-key', encoding='utf-8')
                invalid = rt.reasoning_snapshot()
                self.assertEqual(invalid['status'], 'unavailable_fallback')
                self.assertNotIn('synthetic-key', json.dumps(invalid))
                path.write_text('[]', encoding='utf-8')
                self.assertEqual(rt.reasoning_snapshot()['reason'], 'invalid_settings')

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
