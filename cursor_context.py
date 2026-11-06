"""Optional per-request cursor reader. Only the OpenAI Realtime path calls it.

The separately licensed, short-lived OpenLess reader exchanges text over anonymous
pipes. Nothing here reads controls, waits on the audio path, or logs their text.
"""
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time

from credential_store import ROOT
from realtime_profile import profile_dir, read_json, settings

HELPER = ROOT / 'cursor_reader' / 'OpenLessCursorReader.exe'
BUDGET = 600
TIMEOUT_S = 1.0  # OpenLess's outer Windows capture deadline; never awaited for model readiness.
REASONS = frozenset(('target_changed', 'secure_text_field', 'blocked_app', 'inactive_caret',
    'no_caret_text_pattern', 'no_single_caret_selection', 'non_collapsed_selection',
    'inconsistent_range_movement', 'inconsistent_range_endpoints', 'possibly_truncated_range',
    'invalid_utf16', 'empty_context', 'focus_changed', 'process_unavailable', 'uia_error',
    'com_unavailable', 'bad_request', 'null_uia_object'))


def policy(directory=None):
    preferences = read_json((directory or profile_dir()) / 'preferences.json', {})
    config = settings()
    return {'enabled': config.get('cursor_context_enabled') is True,
            'openless_enabled': isinstance(preferences, dict) and preferences.get('cursorContextEnabled') is True,
            'reader_available': HELPER.is_file(), 'budget_chars': BUDGET,
            'implementation': 'OpenLess-v2.0.1-UIA-contract'}


def foreground_identity():
    """Bind the helper before it starts; no UIA or document reads in this process."""
    if os.name != 'nt':
        return None
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    window = user.GetForegroundWindow()
    process = wintypes.DWORD()
    if not window or not user.GetWindowThreadProcessId(window, ctypes.byref(process)):
        return None
    handle = kernel.OpenProcess(0x1000, False, process.value)
    if not handle:
        return None
    try:
        created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
            return None
        return {'hwnd': str(int(window)), 'pid': process.value,
                'created': str(created.dwLowDateTime | (created.dwHighDateTime << 32))}
    finally:
        kernel.CloseHandle(handle)


class CursorTicket:
    def __init__(self, *, enabled, command=None, snapshotter=foreground_identity,
                 still_enabled=lambda: True, timeout_s=TIMEOUT_S):
        self.snapshotter = snapshotter
        self.still_enabled = still_enabled
        self.command = command or [str(HELPER)]
        self.timeout_s = timeout_s
        self.target = None
        self.task = self.process = None
        self.result = None
        self.discarded = False
        self.metadata = {'status': 'disabled', 'forwarded': False, 'before_chars': 0, 'after_chars': 0}
        if not enabled:
            return
        try:
            self.target = snapshotter()
            if not self.target:
                self.metadata['status'] = 'target_unavailable'
                return
            self.metadata['status'] = 'collecting'
            self.task = asyncio.create_task(self._collect())
        except Exception:
            self.metadata['status'] = 'unavailable'

    async def _collect(self):
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout_s):
                options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                self.process = await asyncio.create_subprocess_exec(*self.command, '--read',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, **options)
                request = dict(self.target, budget=BUDGET)
                output, _ = await self.process.communicate((json.dumps(request) + '\n').encode('utf-8'))
                if self.discarded:
                    return
                if self.process.returncode != 0 or len(output) > 16384:
                    raise ValueError('Invalid native reader result')
                value = json.loads(output.decode('utf-8-sig'))
                if not isinstance(value, dict) or value.get('status') not in ('ok', 'blocked', 'unavailable'):
                    raise ValueError('Invalid native reader result')
                if value['status'] != 'ok':
                    self.metadata.update(status=value['status'], reason=value.get('reason') if value.get('reason') in REASONS else 'uia_error')
                    return
                before, after, block = (value.get(k) for k in ('before', 'after', 'prompt_block'))
                if not all(isinstance(v, str) for v in (before, after, block)):
                    raise ValueError('Invalid cursor fields')
                if len(before) + len(after) > BUDGET or len(block) > 6000 or not block.startswith('# 光标上下文'):
                    raise ValueError('Invalid cursor bounds')
                for text in (before, after, block):
                    text.encode('utf-16-le', errors='strict')
                if not (before.strip() or after.strip()):
                    self.metadata['status'] = 'empty'
                    return
                self.result = block
                self.metadata.update(status='ready', before_chars=len(before), after_chars=len(after),
                    source=value.get('source') if value.get('source') in ('textPattern2', 'textPattern') else 'unknown')
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if not self.discarded:
                self.metadata['status'] = 'timeout'
        except Exception:
            if not self.discarded:
                self.metadata['status'] = 'unavailable'
        finally:
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()
            if not self.discarded:
                self.metadata['capture_ms'] = round((time.monotonic() - started) * 1000, 1)

    def take_ready(self):
        """Never await capture. A fast upstream handshake wins over optional context."""
        try:
            if self.task is None:
                return None
            if not self.still_enabled():
                self.discard('disabled_during_capture')
                return None
            if self.task is not None and not self.task.done():
                self.discard('skipped_not_ready')
                return None
            if self.result is None:
                return None
            if self.snapshotter() != self.target:
                self.discard('focus_changed')
                return None
            block, self.result = self.result, None
            self.metadata.update(status='used', forwarded=True)
            return block
        except Exception:
            self.discard('unavailable')
            return None

    def discard(self, status):
        self.discarded = True
        self.result = None
        self.metadata.update(status=status, forwarded=False, before_chars=0, after_chars=0)

    async def close(self):
        self.result = None
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def begin_capture(directory=None):
    """Gate lazily: no helper process or capture at all for disabled/absent policy."""
    try:
        state = policy(directory)
        enabled = state['enabled'] and state['openless_enabled'] and state['reader_available']
        def still_enabled():
            current = policy(directory)
            return current['enabled'] and current['openless_enabled']
        ticket = CursorTicket(enabled=enabled, still_enabled=still_enabled)
        if not enabled:
            ticket.metadata['status'] = ('disabled' if not state['enabled'] else
                'openless_disabled' if not state['openless_enabled'] else 'reader_missing')
        return ticket
    except Exception:
        ticket = CursorTicket(enabled=False)
        ticket.metadata['status'] = 'unavailable'
        return ticket
