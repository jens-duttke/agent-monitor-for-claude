"""Tests for settings validation (unknown-key reporting)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import settings


class ValidateTest(unittest.TestCase):
    def test_unknown_key_is_reported_and_dropped(self) -> None:
        # A misspelled key (the most common hand-edit mistake) must be reported,
        # like a type error on a known key - not silently ignored.
        with mock.patch('agent_monitor_for_claude.settings._error_dialog') as dialog:
            result = settings._validate({'pollinterval': 3, 'poll_interval': 5}, Path('x'))

        self.assertNotIn('pollinterval', result)
        self.assertEqual(result.get('poll_interval'), 5)
        dialog.assert_called_once()
        self.assertIn('pollinterval', dialog.call_args[0][0])

    def test_underscore_key_is_tolerated_as_a_comment(self) -> None:
        with mock.patch('agent_monitor_for_claude.settings._error_dialog') as dialog:
            result = settings._validate({'_comment': 'notes', 'poll_interval': 5}, Path('x'))

        self.assertEqual(result.get('poll_interval'), 5)
        dialog.assert_not_called()

    def test_known_valid_keys_pass_untouched(self) -> None:
        with mock.patch('agent_monitor_for_claude.settings._error_dialog') as dialog:
            data = {'poll_interval': 5, 'language': 'de', 'include_completed': True}
            result = settings._validate(dict(data), Path('x'))

        self.assertEqual(result, data)
        dialog.assert_not_called()


if __name__ == '__main__':
    unittest.main()
