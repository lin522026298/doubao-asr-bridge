"""On-demand Windows login notice. Never blocks the ASR event loop."""
import ctypes
import os
import pathlib
import subprocess
import sys

from credential_store import CREDENTIALS

ROOT = pathlib.Path(__file__).resolve().parent
MESSAGES = {
    'login_missing': '尚未保存完整的豆包登录信息，请先登录。',
    'login_expired': '豆包登录已过期，语音识别暂不可用，请重新登录。',
    'login_unreadable': '无法读取豆包登录信息，请重新登录后重试。',
    'login_check_required': '豆包拒绝了当前请求（710022013）。此错误曾因登录过期出现，请重新登录后重试；若仍失败，请检查豆包服务状态。',
    'login_rejected': '豆包拒绝了认证或访问权限，请重新登录后重试；若仍失败，请检查账号权限或服务状态。',
}


def credential_version():
    try:
        info = CREDENTIALS.stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def start_interactive(script, *arguments):
    # A venv pythonw redirector can inherit the hidden launcher's show state.
    # Explicitly show this interactive child without creating a console window.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1  # SW_SHOWNORMAL
    return subprocess.Popen([sys.executable, str(script), *arguments], cwd=ROOT,
                            creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)


class LoginNotifier:
    def __init__(self):
        self.notified = False
        self.version = None
        self.process = None

    def notify(self, reason):
        if reason not in MESSAGES or os.name != 'nt':
            return False
        version = credential_version()
        if self.notified and self.version == version:
            return False
        if self.process is not None and self.process.poll() is None:
            return False
        try:
            self.process = start_interactive(ROOT / 'login_notice.py', reason)
        except OSError:
            return False
        self.version, self.notified = version, True
        return True

    def succeeded(self):
        self.notified = False


def show(reason):
    if os.name != 'nt' or reason not in MESSAGES:
        return
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    message = user32.MessageBoxW
    message.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    message.restype = ctypes.c_int
    text = MESSAGES[reason] + '\n\n现在打开豆包登录窗口吗？\n也可稍后打开安装目录的“登录豆包”快捷方式。'
    # YES opens the existing independent login flow. NO leaves the app available.
    # Foreground only this actionable notice; default to NO to avoid an
    # accidental Enter opening another window while the user is typing.
    answer = message(None, text, '豆包 ASR — 需要检查登录',
                     0x00000004 | 0x00000030 | 0x00000100 | 0x00010000)
    if answer == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if answer == 6:
        start_interactive(ROOT / 'login.py')
    return answer


if __name__ == '__main__':
    show(sys.argv[1] if len(sys.argv) == 2 else '')
