"""Windows current-user DPAPI storage; never exports credentials to logs."""
import ctypes
import json
import pathlib
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / 'data'
CREDENTIALS = STATE / 'session.dpapi'

class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

def transform(raw, encrypt):
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    dll = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = dll.CryptProtectData if encrypt else dll.CryptUnprotectData
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free(ctypes.cast(target.data, ctypes.c_void_p))

def save_session(value):
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = CREDENTIALS.with_suffix('.tmp')
    temporary.write_bytes(transform(json.dumps(value).encode('utf-8'), True))
    temporary.replace(CREDENTIALS)

def load_session():
    return json.loads(transform(CREDENTIALS.read_bytes(), False))
