"""Current-user DPAPI storage for the official OpenAI API key."""
import json
from credential_store import STATE, transform

KEY_FILE = STATE / 'openai.dpapi'


def validate_key(value):
    if not isinstance(value, str) or not value.startswith('sk-') or len(value) < 40 or any(c.isspace() for c in value):
        raise ValueError('Invalid OpenAI API key format')
    return value


def save_key(value, target=KEY_FILE):
    value = validate_key(value)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(transform(json.dumps({'version': 1, 'api_key': value}).encode('utf-8'), True))
    temporary.replace(target)


def load_key(source=KEY_FILE):
    value = json.loads(transform(source.read_bytes(), False))
    return validate_key(value.get('api_key'))


def key_status(source=KEY_FILE):
    if not source.exists():
        return 'missing'
    try:
        load_key(source)
        return 'configured_unverified'
    except Exception:
        return 'unreadable'
