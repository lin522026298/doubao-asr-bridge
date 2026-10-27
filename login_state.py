"""Local login checks. Cookie metadata is evidence of expiry, not server acceptance."""
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from credential_store import CREDENTIALS, STATE, load_session

AUTH_COOKIES = ('sessionid', 'sessionid_ss', 'sid_tt')
PROFILE = Path.home() / 'AppData/Roaming/doubao-murmur/asr_params.json'


class LoginRequired(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def cookie_expiry(morsel, now=None):
    """pywebview returns RFC 1123 expiry strings; session cookies remain unknown."""
    now = time.time() if now is None else now
    try:
        if morsel['max-age']:
            return now + int(morsel['max-age'])
        if morsel['expires']:
            value = parsedate_to_datetime(morsel['expires'])
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            stamp = value.timestamp()
            return stamp if stamp > 0 else None
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def legacy_expiries(auth, saved_at, state=STATE):
    """Read metadata only for old saves made at the same login as this profile.

    Newer/different profile cookies must never invalidate a saved session. If the
    creation time cannot be correlated within a minute, leave expiry unknown.
    No cookie values, encrypted values or browser keys are read from SQLite.
    """
    expected = {name for name in AUTH_COOKIES if auth.get('cookies', {}).get(name)}
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (state / 'WebView2').rglob('Cookies'):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                rows = db.execute("SELECT name, creation_utc, expires_utc FROM cookies "
                                  "WHERE host_key IN ('.doubao.com', 'doubao.com', 'www.doubao.com') "
                                  "AND name IN ('sessionid', 'sessionid_ss', 'sid_tt')").fetchall()
            found = {}
            for name, created, expires in rows:
                created = epoch + created / 1_000_000
                if name in expected and saved_at - 60 <= created <= saved_at + 5 and expires > 0:
                    found[name] = max(found.get(name, 0), epoch + expires / 1_000_000)
            if expected and expected <= found.keys():
                return found
        except (OSError, sqlite3.Error, ValueError, OverflowError):
            continue
    return {}


def assess_session(auth, now=None):
    now = time.time() if now is None else now
    if not isinstance(auth, dict) or not isinstance(auth.get('cookies'), dict):
        raise LoginRequired('login_unreadable')
    names = [name for name in AUTH_COOKIES if auth['cookies'].get(name)]
    if not names or not auth.get('device_id') or not auth.get('web_id'):
        raise LoginRequired('login_missing')
    expiries = auth.get('cookie_expiries') or {}
    known = [expiries.get(name) for name in names]
    if all(isinstance(value, (int, float)) and value > 0 and value <= now for value in known):
        raise LoginRequired('login_expired')
    return {'status': 'saved_unverified', 'server_validated': False,
            'expiry_known': all(isinstance(value, (int, float)) and value > 0 for value in known)}


def load_auth():
    try:
        if CREDENTIALS.exists():
            auth = load_session()
            if isinstance(auth, dict) and not auth.get('cookie_expiries'):
                auth['cookie_expiries'] = legacy_expiries(auth, CREDENTIALS.stat().st_mtime)
        elif os.environ.get('DOUBAO_BRIDGE_ALLOW_MURMUR') == '1':
            auth = json.loads(PROFILE.read_text(encoding='utf-8-sig'))
        else:
            raise LoginRequired('login_missing')
        assess_session(auth)
        return auth
    except LoginRequired:
        raise
    except Exception:
        # DPAPI/read/parse exceptions may contain local paths; return no raw text.
        raise LoginRequired('login_unreadable') from None


def local_login_status():
    try:
        return assess_session(load_auth())
    except LoginRequired as exc:
        return {'status': exc.reason, 'server_validated': False, 'expiry_known': exc.reason == 'login_expired'}
