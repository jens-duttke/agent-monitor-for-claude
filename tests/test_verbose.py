"""Tests for the verbose-diagnostics helpers (home-path redaction)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import verbose


class RedactHomeTest(unittest.TestCase):
    def _redact(self, path_str: str, home: str = 'C:\\Users\\jens') -> str:
        with mock.patch('agent_monitor_for_claude.verbose.Path.home', return_value=Path(home)):
            return verbose._redact_home(path_str)

    def test_redacts_exact_home_and_a_prefix(self) -> None:
        self.assertEqual(self._redact('C:\\Users\\jens'), '~')
        self.assertEqual(self._redact('C:\\Users\\jens\\.claude'), '~\\.claude')

    def test_redacts_case_insensitively(self) -> None:
        # A hand-typed lower-case path must still be redacted on case-insensitive
        # NTFS - otherwise the full home path (and the username) is printed.
        self.assertEqual(self._redact('c:\\users\\jens\\.claude'), '~\\.claude')

    def test_does_not_over_match_a_sibling_directory(self) -> None:
        # 'jens2' merely starts with 'jens'; it must not be redacted to '~2'.
        self.assertEqual(self._redact('C:\\Users\\jens2\\project'), 'C:\\Users\\jens2\\project')

    def test_leaves_an_unrelated_path_untouched(self) -> None:
        self.assertEqual(self._redact('D:\\Work\\proj'), 'D:\\Work\\proj')


if __name__ == '__main__':
    unittest.main()
