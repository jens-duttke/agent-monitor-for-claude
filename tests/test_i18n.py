"""Tests for language detection and locale completeness."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from agent_monitor_for_claude.i18n import LOCALE_DIR, T, available_languages, detect_lang_code, reload_translations
from agent_monitor_for_claude.settings import Settings


class DetectLangTest(unittest.TestCase):
    def test_iso_locale(self) -> None:
        self.assertEqual(detect_lang_code('de_DE'), 'de')

    def test_windows_locale_name(self) -> None:
        self.assertEqual(detect_lang_code('German_Germany'), 'de')

    def test_windows_legacy_ukrainian(self) -> None:
        self.assertEqual(detect_lang_code('Ukrainian_Ukraine'), 'uk')

    def test_windows_legacy_names_locale_normalize_misses(self) -> None:
        # locale.normalize does not rewrite these legacy Windows names to an ISO
        # code, so they must be mapped by hand to the shipped locale files.
        self.assertEqual(detect_lang_code('Hindi_India'), 'hi')
        self.assertEqual(detect_lang_code('Indonesian_Indonesia'), 'id')
        self.assertEqual(detect_lang_code('Chinese (Simplified)_China'), 'zh-CN')
        self.assertEqual(detect_lang_code('Chinese (Traditional)_Taiwan'), 'zh-TW')

    def test_windows_legacy_chinese_by_region(self) -> None:
        # The unqualified 'Chinese_<Country>' form is disambiguated by country.
        self.assertEqual(detect_lang_code('Chinese_China'), 'zh-CN')
        self.assertEqual(detect_lang_code('Chinese_Taiwan'), 'zh-TW')

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


class LanguageSettingTest(unittest.TestCase):
    def test_every_locale_file_is_offered_under_its_own_name(self) -> None:
        languages = available_languages()

        self.assertEqual([language['code'] for language in languages], sorted(path.stem for path in LOCALE_DIR.glob('*.json')))
        names = [language['name'] for language in languages]
        self.assertEqual(len(set(names)), len(names))
        self.assertNotIn('', names)
        self.assertIn('Deutsch', names)

    def test_a_reload_refills_the_shared_table_in_place(self) -> None:
        german = json.loads((LOCALE_DIR / 'de.json').read_text(encoding='utf-8'))
        before = T
        try:
            with mock.patch('agent_monitor_for_claude.settings.current_settings', return_value=Settings(language='de')):
                reload_translations()
            self.assertIs(T, before)
            self.assertEqual(T['cancel'], german['cancel'])
        finally:
            reload_translations()


if __name__ == '__main__':
    unittest.main()
