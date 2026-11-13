"""Script normalization must not rewrite identifiers, layout or English content."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import realtime_profile as profile
from realtime_text import normalize_output


class TextTests(unittest.TestCase):
    def test_ambiguous_me_variants_become_standard_simplified(self):
        value, meta = normalize_output('這個功能的目的是什麼？還有什麽？', {})
        self.assertEqual(value, '这个功能的目的是什么？还有什么？')
        self.assertEqual(meta['status'], 'ok')

    def test_english_numbers_negation_and_layout_are_preserved(self):
        value, _ = normalize_output('第一，不刪除原始錄音。\n\n第二，GPT-6.1 Sol / API / JSON，7.5 小時，0.8 元。', {})
        self.assertEqual(value, '第一，不删除原始录音。\n\n第二，GPT-6.1 Sol / API / JSON，7.5 小时，0.8 元。')

    def test_invisible_space_artifacts_are_removed_without_breaking_emoji_or_code(self):
        value, _ = normalize_output('\ufeff什\u200b\u200b麼。👩\u200d💻 `A\u200bB`', {})
        self.assertEqual(value, '什么。👩\u200d💻 `A\u200bB`')

    def test_literal_code_paths_and_urls_are_preserved(self):
        original = '說明：`變數`\n```\n變數 = 1\n```\nhttps://example.test/繁體 C:\\資料\\繁體.txt 其他說明。'
        value, _ = normalize_output(original, {})
        self.assertEqual(value, original.replace('說明', '说明'))

    def test_disabled_and_converter_failure_keep_recognition(self):
        original = '這是原文。'
        self.assertEqual(normalize_output(original, {'prefer_simplified': False})[0], original)
        with patch('realtime_text.converter', side_effect=RuntimeError('synthetic')):
            value, meta = normalize_output(original, {})
        self.assertEqual(value, original)
        self.assertEqual(meta['status'], 'unavailable')

    def test_bridge_vocabulary_is_bounded_and_does_not_write_openless_dictionary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'preferences.json').write_text('{}', encoding='utf-8')
            original = json.dumps([{'phrase': 'ExistingTerm'}])
            (root/'dictionary.json').write_text(original, encoding='utf-8')
            with patch.object(profile, 'settings', return_value={'extra_dictionary': ['GPT-6.1 Sol', 'bad\ncommand', 42]}):
                prompt, meta = profile.prompt_snapshot(root)
            self.assertIn('GPT-6.1 Sol', prompt)
            self.assertNotIn('bad\ncommand', prompt)
            self.assertEqual(meta['profile_dictionary_entries'], 1)
            self.assertEqual((root/'dictionary.json').read_text(encoding='utf-8'), original)


if __name__ == '__main__':
    unittest.main()
