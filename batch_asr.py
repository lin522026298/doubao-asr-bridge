"""Isolated batch segments and an explicit completeness contract."""
import asyncio
import audioop
import json
import logging
import math
import time
from logging.handlers import RotatingFileHandler

from asr_errors import RecognitionFailure, safe_diagnostics
from credential_store import STATE

SEGMENT_TIMEOUT_ALLOWANCE = 25
BATCH_TIMEOUT_ALLOWANCE = 40


async def collect_segments(pcm, split, recognize, describe_error, audio_offset_seconds=0,
                           batch_timeout=None, segment_timeout_allowance=None):
    if not math.isfinite(audio_offset_seconds) or audio_offset_seconds < 0:
        raise ValueError('Invalid audio_offset_seconds')
    chunks = split(pcm)
    if not chunks:
        raise ValueError('Empty audio')
    allowance = SEGMENT_TIMEOUT_ALLOWANCE if segment_timeout_allowance is None else segment_timeout_allowance
    timeout = len(pcm) / 32000 + BATCH_TIMEOUT_ALLOWANCE if batch_timeout is None else batch_timeout
    limiter = asyncio.Semaphore(3)
    started = time.monotonic()
    ranges = []
    position = 0
    for index, chunk in enumerate(chunks):
        ranges.append({'index': index, 'start_seconds': audio_offset_seconds + position / 32000,
                       'end_seconds': audio_offset_seconds + (position + len(chunk)) / 32000,
                       'audio_seconds': len(chunk) / 32000})
        position += len(chunk)
    if position != len(pcm):
        raise ValueError('Segment coverage mismatch')

    async def one(index, chunk):
        row = dict(ranges[index])
        begun = time.monotonic()
        try:
            async with limiter:
                # Only mathematical digital silence is safe to omit. Low RMS,
                # room noise or an empty ASR response does not prove no speech.
                if audioop.max(chunk, 2) == 0:
                    row.update(status='silence', text='', completion='digital_silence')
                else:
                    result = await asyncio.wait_for(recognize(chunk), row['audio_seconds'] + allowance)
                    if not isinstance(result.get('text'), str) or not result['text'].strip():
                        raise RecognitionFailure('no_transcript')
                    row.update(status='ok', text=result['text'], completion=result.get('completion', 'unknown'),
                               diagnostics=safe_diagnostics(result.get('diagnostics')))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                exc = RecognitionFailure('recognition_timeout')
            row.update(status='failed', error=describe_error(exc))
        row['elapsed_seconds'] = round(time.monotonic() - begun, 3)
        return row

    tasks = [asyncio.create_task(one(index, chunk)) for index, chunk in enumerate(chunks)]
    try:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        rows = []
        for index, task in enumerate(tasks):
            if task in pending:
                row = dict(ranges[index], status='failed',
                           error=describe_error(RecognitionFailure('batch_timeout')),
                           elapsed_seconds=round(time.monotonic() - started, 3))
            else:
                row = task.result()
            rows.append(row)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    failed = [row for row in rows if row['status'] == 'failed']
    accounted = len(rows) - len(failed)
    missing = [{'segment_index': row['index'], 'start_seconds': row['start_seconds'],
                'end_seconds': row['end_seconds'], 'code': row['error']['code'],
                'retryable': row['error'].get('retryable', False),
                'recovery': row['error'].get('recovery', 'inspect_diagnostics')} for row in failed]
    complete = not failed
    partial = bool(failed and accounted)
    result = {'text': ' '.join(row['text'] for row in rows if row['status'] == 'ok'),
              'audio_seconds': len(pcm) / 32000, 'audio_offset_seconds': audio_offset_seconds,
              'elapsed_seconds': round(time.monotonic() - started, 3), 'segments': len(rows),
              'segment_results': rows, 'missing_ranges': missing, 'complete': complete, 'partial': partial,
              'status': 'complete' if complete else ('partial' if partial else 'failed'),
              'recognized_segments': sum(row['status'] == 'ok' for row in rows),
              'silence_segments': sum(row['status'] == 'silence' for row in rows),
              'failed_segments': len(failed),
              'completion': 'all_segments_accounted_for' if complete else 'missing_segments'}
    if failed:
        errors = [row['error'] for row in failed]
        if accounted:
            error = {'code': 'partial_transcription', 'message': '仅部分子段完成识别；请检查 missing_ranges，不能将本结果当作完整转写。',
                     'retryable': any(item.get('retryable', False) for item in errors), 'recovery': 'recover_missing_ranges'}
        elif len({item['code'] for item in errors}) == 1:
            error = dict(errors[0])
        else:
            error = {'code': 'batch_failed', 'message': '所有子段均未完成识别；请检查逐段诊断。',
                     'retryable': any(item.get('retryable', False) for item in errors), 'recovery': 'inspect_segment_results'}
        result['error'] = error
    return result


def response_status(result, allow_partial=False):
    if result['complete'] or result['partial'] and allow_partial:
        return 200
    if not result['partial'] and result['error']['code'] == 'no_transcript':
        return 422
    return 502


def diagnostic_summary(result):
    """Strict whitelist for persisted metadata; never serialize the result wholesale."""
    return {'request_id': result['request_id'], 'status': result['status'],
            'audio_seconds': result['audio_seconds'], 'audio_offset_seconds': result['audio_offset_seconds'],
            'elapsed_seconds': result['elapsed_seconds'],
            'segments': [{'index': row['index'], 'start_seconds': row['start_seconds'],
                          'end_seconds': row['end_seconds'], 'status': row['status'],
                          'elapsed_seconds': row['elapsed_seconds'],
                          'characters': len(row.get('text', '')),
                          'code': row.get('error', {}).get('code'),
                          'diagnostics': safe_diagnostics(row.get('error', {}).get('diagnostics', row.get('diagnostics', {})))}
                         for row in result['segment_results']]}


def log_incomplete_batch(result):
    if result['complete']:
        return
    try:
        logger = logging.getLogger('doubao_bridge.batch_diagnostics')
        if not logger.handlers:
            STATE.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(STATE / 'batch-diagnostics.jsonl', maxBytes=1_000_000,
                                          backupCount=1, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(json.dumps(diagnostic_summary(result), ensure_ascii=False))
    except (OSError, ValueError):
        # Diagnostics must not turn a useful partial result into another failure.
        pass
