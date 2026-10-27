"""Login expiry/error regressions. No real credentials, dialogs or cloud traffic."""
import asyncio
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest
import wave
from contextlib import closing
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bridge
import login_notice
import login_state


def auth(expiries=None):
    value = {'cookies': {'sessionid': 'TEST_ONLY'}, 'device_id': 'test', 'web_id': 'test'}
    if expiries is not None:
        value['cookie_expiries'] = expiries
    return value


class LocalChecks(unittest.TestCase):
    def test_expired_cookies_require_login(self):
        with self.assertRaises(login_state.LoginRequired) as caught:
            login_state.assess_session(auth({'sessionid': 99}), now=100)
        self.assertEqual(caught.exception.reason, 'login_expired')

    def test_future_or_unknown_expiry_is_not_authenticated(self):
        for value in (auth(), auth({'sessionid': None}), auth({'sessionid': 101})):
            status = login_state.assess_session(value, now=100)
            self.assertEqual(status['status'], 'saved_unverified')
            self.assertFalse(status['server_validated'])

    def test_valid_alternative_cookie_prevents_false_expiry(self):
        value = auth({'sessionid': 99, 'sid_tt': 101})
        value['cookies']['sid_tt'] = 'TEST_ONLY'
        self.assertEqual(login_state.assess_session(value, now=100)['status'], 'saved_unverified')

    def test_malformed_and_incomplete_sessions(self):
        for value in (None, [], {'cookies': []}, {'cookies': {}}):
            with self.assertRaises(login_state.LoginRequired):
                login_state.assess_session(value)

    def test_cookie_expiry_parser(self):
        jar = SimpleCookie('sessionid=test; expires=Wed, 21 Oct 2026 10:54:37 GMT')
        self.assertEqual(login_state.cookie_expiry(jar['sessionid']), 1792580077)
        jar['sessionid']['expires'] = 'invalid'
        self.assertIsNone(login_state.cookie_expiry(jar['sessionid']))
        jar['sessionid']['max-age'] = '30'
        self.assertEqual(login_state.cookie_expiry(jar['sessionid'], now=100), 130)

    def test_legacy_profile_metadata_must_match_save_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = pathlib.Path(temporary)
            path = state / 'WebView2' / 'Default' / 'Network' / 'Cookies'
            path.parent.mkdir(parents=True)
            # Chromium's epoch is 11644473600 seconds before Unix's epoch.
            chromium = lambda stamp: int((stamp + 11644473600) * 1_000_000)
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE cookies (host_key TEXT, name TEXT, creation_utc INTEGER, expires_utc INTEGER)')
                db.execute('INSERT INTO cookies VALUES (?, ?, ?, ?)',
                           ('.doubao.com', 'sessionid', chromium(100), chromium(200)))
                db.commit()
            self.assertEqual(login_state.legacy_expiries(auth(), 103, state), {'sessionid': 200})
            # A newer/different login must not invalidate the old DPAPI save.
            self.assertEqual(login_state.legacy_expiries(auth(), 1, state), {})
            self.assertEqual(login_state.legacy_expiries(auth(), 200, state), {})

    def test_unreadable_dpapi_never_leaks_exception(self):
        with patch.object(login_state.CREDENTIALS.__class__, 'exists', return_value=True), \
                patch.object(login_state, 'load_session', side_effect=RuntimeError('SECRET_URL')):
            status = login_state.local_login_status()
        self.assertEqual(status['status'], 'login_unreadable')
        self.assertNotIn('SECRET', json.dumps(status))

    def test_notice_deduplicates_until_relogin_or_recovery(self):
        process = Mock()
        process.poll.return_value = 0
        with patch.object(login_notice, 'credential_version', return_value=(1, 10)) as version, \
                patch.object(login_notice.subprocess, 'Popen', return_value=process) as spawn:
            notifier = login_notice.LoginNotifier()
            self.assertTrue(notifier.notify('login_expired'))
            self.assertFalse(notifier.notify('login_check_required'))
            version.return_value = (2, 10)
            self.assertTrue(notifier.notify('login_expired'))
            notifier.succeeded()
            self.assertTrue(notifier.notify('login_expired'))
            self.assertFalse(notifier.notify('asr_failed'))
            self.assertEqual(spawn.call_count, 3)
            self.assertEqual(spawn.call_args.args[0][-1], 'login_expired')

    def test_open_notice_does_not_stack(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(login_notice, 'credential_version', side_effect=[(1, 1), (2, 1)]), \
                patch.object(login_notice.subprocess, 'Popen', return_value=process) as spawn:
            notifier = login_notice.LoginNotifier()
            self.assertTrue(notifier.notify('login_expired'))
            self.assertFalse(notifier.notify('login_expired'))
            spawn.assert_called_once()


class ErrorChecks(unittest.TestCase):
    def setUp(self):
        self.notifier = patch.object(bridge, 'NOTIFIER').start()
        self.addCleanup(patch.stopall)

    def test_network_errors_do_not_trigger_login_popup_or_leak_url(self):
        for exc in (TimeoutError('SECRET_URL'), ConnectionError('SECRET_COOKIE')):
            details = bridge.failure_details(exc)
            self.assertEqual(details['code'], 'asr_failed')
            self.assertNotIn('SECRET', json.dumps(details))
        self.notifier.notify.assert_not_called()

    def test_observed_upstream_code_is_hint_not_proof_of_expiry(self):
        details = bridge.failure_details(bridge.UpstreamRejected(710022013))
        self.assertEqual(details['code'], 'login_check_required')
        self.assertIn('曾因登录过期', details['message'])
        self.notifier.notify.assert_called_once_with('login_check_required')

    def test_unknown_upstream_codes_are_safe_and_do_not_prompt(self):
        for code in (12345, 'https://SECRET_URL'):
            details = bridge.failure_details(bridge.UpstreamRejected(code))
            self.assertEqual(details['code'], 'upstream_rejected')
            self.assertNotIn('SECRET', json.dumps(details))
        self.notifier.notify.assert_not_called()

    def test_auth_denial_uses_permission_wording(self):
        error = aiohttp.WSServerHandshakeError(None, (), status=403)
        details = bridge.failure_details(error)
        self.assertEqual(details['code'], 'login_rejected')
        self.assertIn('权限', details['message'])


class ProtocolChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.notifier = patch.object(bridge, 'NOTIFIER').start()
        self.addCleanup(patch.stopall)
        bridge.LOCK = asyncio.Lock()

    async def test_startup_prompts_expiry_but_starts_service(self):
        with patch.object(bridge, 'local_login_status', return_value={'status': 'login_expired'}):
            app = web.Application()
            app.on_startup.append(bridge.check_login_on_startup)
            app.router.add_get('/ready', lambda _: web.json_response({'ok': True}))
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get('/ready')).status, 200)
        self.notifier.notify.assert_called_once_with('login_expired')

    async def test_expired_stream_sends_failed_not_partial_success(self):
        async def reject(*args, **kwargs):
            await kwargs['partial']('temporary partial')
            raise login_state.LoginRequired('login_expired')
        with patch.object(bridge, 'recognize', reject):
            app = web.Application()
            app.router.add_get('/realtime', bridge.realtime)
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect('/realtime')
                await ws.send_json({'type': 'session.update'})
                self.assertEqual((await ws.receive_json())['type'], 'session.updated')
                await ws.send_json({'type': 'session.finish'})
                events = [await ws.receive_json(), await ws.receive_json()]
                self.assertEqual(events[-1]['type'], 'conversation.item.input_audio_transcription.failed')
                self.assertEqual(events[-1]['error']['code'], 'login_expired')
                await ws.close()
        self.notifier.notify.assert_called_once_with('login_expired')
        self.assertFalse(bridge.LOCK.locked())

    async def test_expired_batch_has_actionable_error(self):
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(3200))
        async def reject(*args):
            raise login_state.LoginRequired('login_expired')
        with patch.object(bridge, 'recognize_segments', reject):
            app = web.Application()
            app.router.add_post('/transcribe', bridge.transcribe)
            async with TestClient(TestServer(app)) as client:
                form = aiohttp.FormData()
                form.add_field('file', data.getvalue(), filename='test.wav', content_type='audio/wav')
                response = await client.post('/transcribe', data=form)
                self.assertEqual(response.status, 502)
                self.assertEqual((await response.json())['error']['code'], 'login_expired')

    async def test_receiver_preserves_rejection_for_stream_and_batch(self):
        class Upstream:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def send_bytes(self, chunk):
                pass
            async def __aiter__(self):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({'code': 710022013}))
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def ws_connect(self, *args, **kwargs):
                return Upstream()
        async def audio():
            yield bytes(3200)
            await asyncio.sleep(.01)
            yield bytes(3200)
        with patch.object(bridge, 'load_auth', return_value=auth()), patch.object(bridge, 'ClientSession', Session):
            for stream in (None, audio()):
                with self.assertRaises(bridge.UpstreamRejected) as caught:
                    await bridge.recognize(bytes(6400), speed=100, stream=stream)
                self.assertEqual(caught.exception.code, '710022013')


if __name__ == '__main__':
    unittest.main(verbosity=2)
