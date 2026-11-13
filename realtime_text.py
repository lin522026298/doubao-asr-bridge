"""Final-text script normalization for GPT Realtime only; no model or network call."""
from functools import lru_cache
import re
import time

from realtime_profile import settings

# Keep explicitly delimited code, URLs and Windows paths literal. English, numbers,
# whitespace and punctuation outside these spans are unchanged by OpenCC t2s.
LITERAL = re.compile(r'(```[\s\S]*?```|`[^`\r\n]*`|https?://[^\s<>]+|[A-Za-z]:[\\/][^\s<>"|]+)')


@lru_cache(maxsize=1)
def converter():
    from opencc import OpenCC
    return OpenCC('t2s')


def normalize_output(text, config=None):
    config = settings() if config is None else config
    if config.get('prefer_simplified', True) is False:
        return text, {'status': 'disabled', 'changed': False}
    started = time.perf_counter()
    try:
        parts = LITERAL.split(text)
        convert = converter().convert
        result = ''.join(part if i % 2 else convert(part.replace('\u200b', '').replace('\ufeff', ''))
                         for i, part in enumerate(parts))
        return result, {'status': 'ok', 'changed': result != text,
                        'processing_ms': round((time.perf_counter() - started) * 1000, 2)}
    except Exception:
        # An optional display preference must never discard valid recognition.
        return text, {'status': 'unavailable', 'changed': False}
