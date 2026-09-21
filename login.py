"""On-demand login in an independent WebView2 profile; no keyboard hooks."""
import json
import threading
import time
from urllib.parse import urlparse
import webview
from credential_store import STATE, save_session

STOP = threading.Event()
IDS = r'''(() => {
  try {
    const d=JSON.parse(localStorage.getItem('samantha_web_web_id')||'{}');
    const w=JSON.parse(localStorage.getItem('__tea_cache_tokens_497858')||'{}');
    return {device_id:String(d.web_id||''),web_id:String(w.web_id||'')};
  } catch(e) { return null; }
})()'''

def monitor(window):
    while not STOP.wait(2):
        try:
            url = window.get_current_url() or ''
            if urlparse(url).hostname != 'www.doubao.com':
                continue
            ids = window.evaluate_js(IDS)
            if not ids or not ids.get('device_id') or not ids.get('web_id'):
                continue
            cookies = {}
            for jar in window.get_cookies():
                for name, morsel in jar.items():
                    domain = morsel['domain'].lstrip('.')
                    if domain == 'doubao.com' or domain.endswith('.doubao.com'):
                        cookies[name] = morsel.value
            if not any(cookies.get(key) for key in ('sessionid','sessionid_ss','sid_tt')):
                continue
            # Presence is not proof of server acceptance. Health state says saved, not validated.
            save_session(dict(ids, cookies=cookies))
            (STATE / 'login-status.json').write_text(json.dumps({'status':'session_saved',
                'server_validated':False}), encoding='utf-8')
            window.set_title('豆包 ASR 兼容工具 — 登录信息已保存，可关闭窗口')
            return
        except Exception as exc:
            # Record type only, never cookies, page content, or network URLs.
            (STATE / 'login-status.json').write_text(json.dumps({'status':'waiting',
                'error_type':type(exc).__name__}), encoding='utf-8')

if __name__ == '__main__':
    STATE.mkdir(parents=True, exist_ok=True)
    window = webview.create_window('豆包 ASR 兼容工具 — 请登录',
                                  'https://www.doubao.com/chat', width=1100, height=780)
    window.events.closed += lambda: STOP.set()
    webview.start(monitor, window, gui='edgechromium', private_mode=False,
                  storage_path=str(STATE/'WebView2'))
