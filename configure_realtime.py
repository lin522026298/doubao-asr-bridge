"""Enable or restore the native-audio profile while OpenLess is closed.

Secrets are never accepted on a command line or printed. An existing DPAPI key
may be imported, or entered in a local masked dialog. Runtime data is gitignored.
"""
import argparse
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import psutil
from credential_store import ROOT, STATE, transform
from configure_openless import read, write
from openai_credentials import KEY_FILE, load_key, save_key, validate_key, key_status
from realtime_profile import STYLE_ID, PROVIDER_ID, SETTINGS, DEFAULT_PROMPT, profile_dir
from bridge_version import __version__ as VERSION

RAW_PROMPT_SHA256 = '8f13bf6ca93b8849b1230b687d2c3cece91753b7d8b0e0d22bfbf821740f172d'


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def require_closed():
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() == 'openless.exe':
            raise RuntimeError('Exit OpenLess before changing its profile')


def read_vault():
    manifest = json.loads(read('credentials.v1'))
    if manifest.get('openless_credentials_storage') != 'chunked':
        raise RuntimeError('Unsupported OpenLess credential storage')
    prefix = 'credentials.v1.chunk.' + (manifest['generation'] + '.' if manifest.get('generation') else '')
    value = json.loads(''.join(read(prefix + str(i)) for i in range(manifest['chunks'])))
    return manifest, value


def write_vault(value):
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    chunks = [payload[i:i+1000] for i in range(0, len(payload), 1000)]
    generation = uuid.uuid4().hex
    for i, chunk in enumerate(chunks):
        write('credentials.v1.chunk.' + generation + '.' + str(i), chunk)
    if ''.join(read('credentials.v1.chunk.' + generation + '.' + str(i)) for i in range(len(chunks))) != payload:
        raise RuntimeError('Credential readback failed; manifest was not changed')
    write('credentials.v1', json.dumps({'openless_credentials_storage': 'chunked', 'version': 1,
                                      'generation': generation, 'chunks': len(chunks)}))


def import_key(source):
    raw = transform(source.read_bytes(), False).decode('utf-8').strip()
    value = raw if raw.startswith('sk-') else json.loads(raw).get('api_key')
    save_key(validate_key(value))


def key_dialog():
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title('OpenLess · OpenAI API 密钥')
    window.geometry('570x190')
    window.configure(padx=20, pady=18)
    ttk.Label(window, text='输入 OpenAI API Key，仅在本机加密保存。').pack(anchor='w')
    variable = tk.StringVar()
    entry = ttk.Entry(window, textvariable=variable, show='*', width=72)
    entry.pack(fill='x', pady=15)
    def save():
        try:
            save_key(variable.get().strip())
        except Exception:
            messagebox.showerror('未保存', '请检查密钥格式和本机写入权限。', parent=window)
            return
        variable.set('')
        window.destroy()
    ttk.Button(window, text='加密保存', command=save).pack(anchor='e')
    entry.focus_set()
    window.mainloop()


def make_style(original, raw_prompt):
    pack = deepcopy(original)
    pack.update(id=STYLE_ID, name='Realtime · 清晰结构',
        description='录音结束后直接得到整理正文，适用于 OpenAI Realtime 语音直出通道。',
        author='OpenLess Voice Bridge', version=VERSION, kind='imported', baseMode='raw',
        prompt=raw_prompt, examples=[], tags=['语音直出', 'Realtime'],
        enabled=True, active=True, recommendedModel='gpt-realtime-2.1',
        createdAt=datetime.now(timezone.utc).isoformat(), updatedAt=datetime.now(timezone.utc).isoformat())
    # Keep written-selection and voice-edit prompts, rather than inheriting Raw's
    # selected-text identity prompt. Only ordinary dictation skips polishing.
    pack['selectionPrompt'] = original.get('selectionPrompt', '')
    pack['voiceEditPrompt'] = original.get('voiceEditPrompt', '')
    return pack


def enable(directory, raw_prompt_file=None):
    require_closed()
    load_key()  # Check only the selected operation's credential; never print it.
    prefs = json.loads((directory / 'preferences.json').read_text(encoding='utf-8-sig'))
    packs = json.loads((directory / 'style-packs.json').read_text(encoding='utf-8-sig'))
    manifest, vault = read_vault()
    selected = next((p for p in packs if p['id'] == prefs['activeStylePackId'] and p['id'] != STYLE_ID), None)
    original = selected or next(p for p in packs if p['id'] == 'builtin.structured')
    raw_prompt = (raw_prompt_file.read_text(encoding='utf-8') if raw_prompt_file
                  else next(p['prompt'] for p in packs if p['id'] == 'builtin.raw'))
    # OpenLess treats a *custom* Raw prompt as an LLM request. Only its exact
    # default Raw literal is passthrough. Pin the verified 2.0.1 fingerprint.
    if hashlib.sha256(raw_prompt.encode('utf-8')).hexdigest() != RAW_PROMPT_SHA256:
        raise RuntimeError('Customized or unsupported Raw prompt; provide the verified default with --raw-prompt-file')
    backup_dir = STATE / 'backups' / ('realtime-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    backup_dir.mkdir(parents=True)
    snapshot = {'preferences': prefs, 'styles': packs, 'credential_manifest': manifest, 'credentials': vault,
                'realtime_settings': json.loads(SETTINGS.read_text(encoding='utf-8')) if SETTINGS.exists() else None}
    backup = backup_dir / 'profile.dpapi'
    backup.write_bytes(transform(json.dumps(snapshot, ensure_ascii=False).encode('utf-8'), True))
    vault = deepcopy(vault)
    vault['providers']['asr'][PROVIDER_ID] = {
        'providerType': 'bailian-qwen3-realtime', 'displayName': 'OpenAI Realtime · 语音直出',
        'baseURL': 'ws://127.0.0.1:18765/v1/realtime/openai', 'model': 'gpt-realtime-2.1',
        'apiKey': 'local-bridge', 'order': 0, 'enabled': True}
    vault['active']['asr'] = PROVIDER_ID
    vault['metadata_revision'] = vault.get('metadata_revision', 0) + 1
    packs = [p for p in deepcopy(packs) if p['id'] != STYLE_ID]
    for p in packs:
        p['active'] = False
    packs.append(make_style(original, raw_prompt))
    prefs = deepcopy(prefs)
    prefs.update(activeAsrProvider=PROVIDER_ID, defaultMode='raw', activeStylePackId=STYLE_ID, pipelineMode='traditional')
    prefs['enabledModes'] = list(dict.fromkeys([*prefs.get('enabledModes', []), 'raw']))
    write_json(SETTINGS, {'enabled': True, 'model': 'gpt-realtime-2.1', 'reasoning': 'low',
                          'use_history': True, 'history_turns': 2, 'version': VERSION})
    try:
        write_vault(vault)
        write_json(directory / 'style-packs.json', packs)
        write_json(directory / 'preferences.json', prefs)
    except Exception:
        write_vault(snapshot['credentials'])
        write_json(directory / 'style-packs.json', snapshot['styles'])
        write_json(directory / 'preferences.json', snapshot['preferences'])
        write_json(SETTINGS, snapshot['realtime_settings'] or {'enabled': False})
        raise
    result = {'version': VERSION, 'active_asr': PROVIDER_ID, 'active_style': STYLE_ID, 'default_mode': 'raw',
              'ordinary_dictation_second_llm': False, 'other_llm_unchanged': vault['active'].get('llm'),
              'key_status': key_status(), 'backup': str(backup), 'profile': str(directory)}
    write_json(STATE / 'realtime-deployment.json', result)
    print(json.dumps(result, ensure_ascii=False))


def restore(directory, source):
    require_closed()
    snapshot = json.loads(transform(source.read_bytes(), False))
    write_vault(snapshot['credentials'])
    write_json(directory / 'style-packs.json', snapshot['styles'])
    write_json(directory / 'preferences.json', snapshot['preferences'])
    write_json(SETTINGS, snapshot.get('realtime_settings') or {'enabled': False})
    print('Restored the saved OpenLess profile; protected API key retained locally.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['enable', 'restore', 'key', 'status'])
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--import-dpapi', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--raw-prompt-file', type=Path)
    args = parser.parse_args()
    if args.import_dpapi:
        import_key(args.import_dpapi)
    if args.action == 'key':
        key_dialog()
    elif args.action == 'status':
        print(json.dumps({'key_status': key_status()}, ensure_ascii=False))
    elif args.action == 'enable':
        enable(args.profile or profile_dir(), args.raw_prompt_file)
    elif args.snapshot:
        restore(args.profile or profile_dir(), args.snapshot)
    else:
        parser.error('restore requires --snapshot')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'message': 'Configuration failed; no credential value is printed.'}))
        raise SystemExit(1)
