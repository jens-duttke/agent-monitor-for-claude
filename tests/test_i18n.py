"""Tests for language detection and locale completeness."""
from __future__ import annotations

import json
import unittest

from agent_monitor_for_claude.i18n import LOCALE_DIR, detect_lang_code


class DetectLangTest(unittest.TestCase):
    def test_iso_locale(self) -> None:
        self.assertEqual(detect_lang_code('de_DE'), 'de')

    def test_windows_locale_name(self) -> None:
        self.assertEqual(detect_lang_code('German_Germany'), 'de')

    def test_unknown_falls_back_to_english(self) -> None:
        self.assertEqual(detect_lang_code('xx_YY'), 'en')


class LocaleKeysetTest(unittest.TestCase):
    def test_all_locales_share_the_english_keyset(self) -> None:
        english = json.loads((LOCALE_DIR / 'en.json').read_text(encoding='utf-8'))
        expected = set(english)

        locale_files = sorted(LOCALE_DIR.glob('*.json'))
        self.assertGreater(len(locale_files), 1)

        for path in locale_files:
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(data), expected, f'{path.name} keys differ from en.json')


if __name__ == '__main__':
    unittest.main()
