"""ASR failures with safe metadata. No upstream URLs, cookies or transcript text."""
import math

ISSUES = {
    'no_transcript': ('上游已处理音频，但没有返回可用文本；不能据此断定没有语音。', False, 'review_audio_or_use_alternative_asr'),
    'upstream_closed': ('上游识别连接在完成前结束。', True, 'retry_range_or_use_alternative_asr'),
    'recognition_timeout': ('该识别子段超时。', True, 'retry_range_or_use_alternative_asr'),
    'batch_timeout': ('批量请求达到截止时间，该子段尚未完成。', True, 'retry_range_or_use_alternative_asr'),
}
DIAGNOSTIC_FIELDS = {'sent_audio_seconds', 'expected_audio_seconds', 'received_messages',
                     'result_events', 'empty_result_events', 'nonempty_result_events',
                     'upstream_finish', 'upstream_closed', 'upstream_close_code', 'elapsed_seconds'}


def safe_diagnostics(values):
    return {key: value for key, value in (values or {}).items()
            if key in DIAGNOSTIC_FIELDS and
            (value is None or isinstance(value, bool) or
             isinstance(value, (int, float)) and math.isfinite(value))}


class RecognitionFailure(RuntimeError):
    def __init__(self, code, diagnostics=None):
        self.code = code
        message, self.retryable, self.recovery = ISSUES[code]
        self.message = message
        self.diagnostics = safe_diagnostics(diagnostics)
        super().__init__(code)

    def details(self):
        return {'code': self.code, 'message': self.message, 'retryable': self.retryable,
                'recovery': self.recovery, 'diagnostics': self.diagnostics}
