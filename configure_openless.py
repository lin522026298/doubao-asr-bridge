"""Opt-in OpenLess Beta.1 channel setup. Run only after exiting OpenLess."""
import datetime
import json
import pathlib
import uuid
import psutil
import win32cred
from credential_store import STATE, transform

SUFFIX = '.com.openless.app'

def read(account):
    return win32cred.CredRead(account + SUFFIX, 1)['CredentialBlob'].decode('utf-16-le')

def write(account, value):
    win32cred.CredWrite({'Type': 1, 'TargetName': account + SUFFIX,
        'UserName': account, 'CredentialBlob': value, 'Persist': 3}, 0)

def main():
    if any((p.info['name'] or '').lower() == 'openless.exe'
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('Exit OpenLess before configuring channels')
    pref = pathlib.Path.home() / 'AppData/Roaming/OpenLess/preferences.json'
    preferences = json.loads(pref.read_text(encoding='utf-8-sig'))
    manifest = json.loads(read('credentials.v1'))
    if manifest.get('openless_credentials_storage') != 'chunked':
        raise RuntimeError('Unsupported credential format; no changes made')
    prefix = 'credentials.v1.chunk.'
    if manifest.get('generation'):
        prefix += manifest['generation'] + '.'
    root = json.loads(''.join(read(prefix + str(i)) for i in range(manifest['chunks'])))
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backup = STATE / 'backups' / stamp
    backup.mkdir(parents=True)
    (backup / 'credentials.dpapi').write_bytes(transform(
        json.dumps({'manifest': manifest, 'root': root}).encode(), True))
    (backup / 'preferences.json').write_bytes(pref.read_bytes())
    entries = root['providers']['asr']
    entries['doubao-web-stream'] = dict(providerType='bailian-qwen3-realtime',
        displayName='豆包网页 ASR · 实时流式', baseURL='ws://127.0.0.1:18765/v1/realtime',
        model='doubao-web-asr', apiKey='local-bridge', order=0, enabled=True)
    entries['doubao-web-batch'] = dict(providerType='openai-compatible',
        displayName='豆包网页 ASR · 批量（录完后处理）', baseURL='http://127.0.0.1:18765/v1',
        model='doubao-web-asr', order=1, enabled=True,
        advancedConfig=json.dumps({'verboseJson': False, 'chunkDurationMs': None}))
    root['active']['asr'] = 'doubao-web-stream'
    root['metadata_revision'] = root.get('metadata_revision', 0) + 1
    payload = json.dumps(root, ensure_ascii=True, separators=(',', ':'))
    chunks = [payload[i:i+1000] for i in range(0, len(payload), 1000)]
    generation = uuid.uuid4().hex
    for i, chunk in enumerate(chunks):
        write('credentials.v1.chunk.' + generation + '.' + str(i), chunk)
    assert ''.join(read('credentials.v1.chunk.' + generation + '.' + str(i))
                   for i in range(len(chunks))) == payload
    write('credentials.v1', json.dumps(dict(openless_credentials_storage='chunked',
        version=1, generation=generation, chunks=len(chunks))))
    preferences['activeAsrProvider'] = 'doubao-web-stream'
    temporary = pref.with_suffix('.bridge.tmp')
    temporary.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(pref)
    print('Configured streaming and batch ASR. Encrypted backup saved locally; LLM unchanged.')

if __name__ == '__main__':
    main()
