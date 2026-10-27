"""Synthetic batch regressions. No credentials, cloud traffic or medical audio."""
import asyncio
import io
import json
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import batch_asr
import bridge
from asr_errors import RecognitionFailure


def pcm(value, seconds=.05):
    return int(value).to_bytes(2, 'little', signed=True) * int(16000 * seconds)


def wav_bytes(data):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(data)
    return output.getvalue()


class AggregationTests(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, chunks, worker, **kwargs):
        return await batch_asr.collect_segments(b''.join(chunks), lambda _: chunks, worker,
                                                bridge.failure_details, **kwargs)

    async def test_failed_segment_keeps_successful_siblings_and_global_offsets(self):
        chunks = [pcm(1), pcm(2), pcm(3)]
        async def worker(data):
            if data == chunks[1]:
                await asyncio.sleep(.001)
                raise RecognitionFailure('no_transcript', {'result_events': 8, 'empty_result_events': 8})
            await asyncio.sleep(.01 if data == chunks[0] else .02)
            return {'text': 'first' if data == chunks[0] else 'third', 'completion': 'finish_event'}
        result = await self.run_batch(chunks, worker, audio_offset_seconds=58)
        self.assertEqual(result['text'], 'first third')
        self.assertFalse(result['complete'])
        self.assertTrue(result['partial'])
        self.assertEqual([row['status'] for row in result['segment_results']], ['ok', 'failed', 'ok'])
        self.assertAlmostEqual(result['missing_ranges'][0]['start_seconds'], 58.05)
        self.assertAlmostEqual(result['missing_ranges'][0]['end_seconds'], 58.1)
        self.assertFalse(result['missing_ranges'][0]['retryable'])
        self.assertEqual(batch_asr.response_status(result), 502)
        self.assertEqual(batch_asr.response_status(result, allow_partial=True), 200)

    async def test_all_no_transcript_is_diagnostic_not_silence_or_login(self):
        async def worker(data):
            raise RecognitionFailure('no_transcript')
        with patch.object(bridge, 'NOTIFIER') as notifier:
            result = await self.run_batch([pcm(3), pcm(4)], worker)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['text'], '')
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['missing_ranges']), 2)
        self.assertEqual(batch_asr.response_status(result, True), 422)
        notifier.notify.assert_not_called()

    async def test_exact_silence_is_accounted_without_upstream(self):
        worker = Mock(side_effect=AssertionError('silence must not be uploaded'))
        result = await self.run_batch([pcm(0), pcm(0)], worker)
        self.assertTrue(result['complete'])
        self.assertEqual(result['silence_segments'], 2)
        self.assertEqual(result['missing_ranges'], [])
        self.assertEqual(batch_asr.response_status(result), 200)
        worker.assert_not_called()

    async def test_very_quiet_nonzero_audio_is_never_silently_discarded(self):
        calls = []
        async def worker(data):
            calls.append(data)
            raise RecognitionFailure('no_transcript')
        result = await self.run_batch([pcm(1)], worker)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['silence_segments'], 0)

    async def test_empty_upstream_text_is_missing_even_without_exception(self):
        async def worker(data):
            return {'text': '   '}
        result = await self.run_batch([pcm(1)], worker)
        self.assertEqual(result['error']['code'], 'no_transcript')
        self.assertEqual(len(result['missing_ranges']), 1)

    async def test_finished_batch_preserves_source_order(self):
        chunks = [pcm(1), pcm(2)]
        async def worker(data):
            if data == chunks[0]:
                await asyncio.sleep(.01)
            return {'text': 'a' if data == chunks[0] else 'b'}
        result = await self.run_batch(chunks, worker)
        self.assertEqual(result['text'], 'a b')
        self.assertTrue(result['complete'])
        self.assertFalse(result['partial'])

    async def test_segment_timeout_does_not_cancel_successful_sibling(self):
        chunks = [pcm(1, .001), pcm(2, .001)]
        cancelled = asyncio.Event()
        async def worker(data):
            if data == chunks[0]:
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            return {'text': 'kept'}
        result = await self.run_batch(chunks, worker, segment_timeout_allowance=.01)
        self.assertEqual(result['text'], 'kept')
        self.assertEqual(result['missing_ranges'][0]['code'], 'recognition_timeout')
        self.assertTrue(result['missing_ranges'][0]['retryable'])
        self.assertTrue(cancelled.is_set())

    async def test_batch_deadline_keeps_completed_rows(self):
        chunks = [pcm(1), pcm(2)]
        async def worker(data):
            if data == chunks[1]:
                await asyncio.sleep(10)
            return {'text': 'kept'}
        result = await self.run_batch(chunks, worker, batch_timeout=.2)
        self.assertEqual(result['text'], 'kept')
        self.assertEqual(result['missing_ranges'][0]['code'], 'batch_timeout')
        self.assertTrue(result['partial'])

    async def test_parent_cancellation_cleans_all_children(self):
        entered, stopped = set(), set()
        async def worker(data):
            entered.add(data)
            try:
                await asyncio.sleep(10)
            finally:
                stopped.add(data)
        task = asyncio.create_task(self.run_batch([pcm(1), pcm(2), pcm(3)], worker))
        for _ in range(100):
            if len(entered) == 3:
                break
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(entered, stopped)

    async def test_diagnostics_never_persist_transcript_or_exception_text(self):
        chunks = [pcm(1), pcm(2)]
        async def worker(data):
            if data == chunks[1]:
                raise RuntimeError('SECRET_CREDENTIAL_URL')
            return {'text': 'PRIVATE_TRANSCRIPT', 'diagnostics': {'url': 'SECRET_URL', 'result_events': 2}}
        result = await self.run_batch(chunks, worker)
        result['request_id'] = 'test-request'
        safe = json.dumps(batch_asr.diagnostic_summary(result))
        self.assertNotIn('PRIVATE', safe)
        self.assertNotIn('SECRET', safe)
        self.assertEqual(result['error']['code'], 'partial_transcription')

    async def test_split_coverage_preserves_all_audio_frames(self):
        # Includes exact silence, a short tail, and ordinary nonzero audio.
        data = pcm(1, 20) + pcm(0, 20) + pcm(2, 21)
        chunks = bridge.split_pcm(data)
        self.assertEqual(b''.join(chunks), data)
        async def worker(chunk):
            return {'text': 'kept'}
        result = await self.run_batch(chunks, worker, audio_offset_seconds=100)
        self.assertAlmostEqual(result['segment_results'][0]['start_seconds'], 100)
        self.assertAlmostEqual(result['segment_results'][-1]['end_seconds'], 161)
        for previous, following in zip(result['segment_results'], result['segment_results'][1:]):
            self.assertAlmostEqual(previous['end_seconds'], following['start_seconds'])


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bridge.LOCK = asyncio.Lock()
        self.logger = patch.object(batch_asr, 'log_incomplete_batch').start()
        self.addCleanup(patch.stopall)

    def form(self, controls=None, audio=None):
        value = aiohttp.FormData()
        value.add_field('file', wav_bytes(pcm(1) if audio is None else audio), filename='sample.wav', content_type='audio/wav')
        for key, item in (controls or {}).items():
            value.add_field(key, item)
        return value

    async def test_http_strict_default_rejects_partial_but_retains_text(self):
        chunks = [pcm(1), pcm(2)]
        async def worker(data):
            if data == chunks[0]:
                raise RecognitionFailure('no_transcript')
            return {'text': 'kept'}
        with patch.object(bridge, 'split_pcm', return_value=chunks), patch.object(bridge, 'recognize', worker):
            app = web.Application()
            app.router.add_post('/transcribe', bridge.transcribe)
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/transcribe', data=self.form(audio=b''.join(chunks)))
                value = await response.json()
        self.assertEqual(response.status, 502)
        self.assertEqual(value['text'], 'kept')
        self.assertEqual(value['status'], 'partial')
        self.assertFalse(value['complete'])
        self.assertIn('request_id', value)
        self.assertFalse(bridge.LOCK.locked())

    async def test_opt_in_partial_has_explicit_missing_ranges_and_offset(self):
        chunks = [pcm(1), pcm(2)]
        async def worker(data):
            if data == chunks[1]:
                raise RecognitionFailure('no_transcript')
            return {'text': 'kept'}
        with patch.object(bridge, 'split_pcm', return_value=chunks), patch.object(bridge, 'recognize', worker):
            app = web.Application()
            app.router.add_post('/transcribe', bridge.transcribe)
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/transcribe', data=self.form({'allow_partial': 'true', 'audio_offset_seconds': '58'}, b''.join(chunks)))
                value = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(value['allow_partial'])
        self.assertFalse(value['complete'])
        self.assertTrue(value['partial'])
        self.assertAlmostEqual(value['missing_ranges'][0]['start_seconds'], 58.05)

    async def test_bad_controls_and_empty_audio_are_400(self):
        app = web.Application()
        app.router.add_post('/transcribe', bridge.transcribe)
        async with TestClient(TestServer(app)) as client:
            for controls, audio in (({'audio_offset_seconds':'nan'}, None),
                                    ({'audio_offset_seconds':'-1'}, None),
                                    ({'allow_partial':'maybe'}, None), ({}, b'')):
                response = await client.post('/transcribe', data=self.form(controls, audio))
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())['error']['code'], 'bad_request')

    async def test_no_transcript_is_422_and_does_not_prompt_login(self):
        async def worker(data):
            raise RecognitionFailure('no_transcript')
        with patch.object(bridge, 'recognize', worker), patch.object(bridge, 'NOTIFIER') as notifier:
            app = web.Application()
            app.router.add_post('/transcribe', bridge.transcribe)
            async with TestClient(TestServer(app)) as client:
                response = await client.post('/transcribe', data=self.form({'allow_partial':'true'}))
                value = await response.json()
        self.assertEqual(response.status, 422)
        self.assertEqual(value['error']['code'], 'no_transcript')
        notifier.notify.assert_not_called()


class UpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_result_events_remain_distinct_from_early_close(self):
        class Upstream:
            close_code = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def send_bytes(self, data):
                pass
            async def __aiter__(self):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data='{"event":"result","result":{"Text":""}}')
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data='{"event":"finish"}')
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def ws_connect(self, *args, **kwargs):
                return Upstream()
        with patch.object(bridge, 'load_auth', return_value={'cookies':{}, 'device_id':'test', 'web_id':'test'}), \
                patch.object(bridge, 'ClientSession', Session):
            with self.assertRaises(RecognitionFailure) as caught:
                await bridge.recognize(pcm(1), speed=100)
        self.assertEqual(caught.exception.code, 'no_transcript')
        self.assertEqual(caught.exception.diagnostics['result_events'], 1)
        self.assertEqual(caught.exception.diagnostics['empty_result_events'], 1)
        self.assertTrue(caught.exception.diagnostics['upstream_finish'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
