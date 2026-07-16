"""Tests for the verbose-diagnostics helpers (home-path redaction)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import verbose


class _FakeStream:
    """A stand-in for a real, connected stream (has a valid fileno)."""

    def fileno(self) -> int:
        return 3

    def write(self, _text: str) -> int:
        return 0


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


class DotnetVersionTest(unittest.TestCase):
    def test_non_dword_release_does_not_crash(self) -> None:
        # A damaged registry can hold a non-DWORD Release; comparing it against
        # the integer thresholds must not raise an uncaught TypeError at startup.
        with mock.patch('agent_monitor_for_claude.verbose.winreg.OpenKey'), \
             mock.patch('agent_monitor_for_claude.verbose.winreg.QueryValueEx', return_value=('not-a-dword', 1)):
            self.assertEqual(verbose._dotnet_version(), 'not found')

    def test_valid_release_maps_to_a_version(self) -> None:
        with mock.patch('agent_monitor_for_claude.verbose.winreg.OpenKey'), \
             mock.patch('agent_monitor_for_claude.verbose.winreg.QueryValueEx', return_value=(533320, 1)):
            self.assertEqual(verbose._dotnet_version(), '4.8.1 (release 533320)')


class SetupConsoleTest(unittest.TestCase):
    def test_usable_streams_are_not_clobbered(self) -> None:
        # A console session or a redirected file must be left in place, or the
        # console buffer would swallow output meant for the redirect target.
        out, err = _FakeStream(), _FakeStream()
        with mock.patch.object(verbose.sys, 'stdout', out), \
             mock.patch.object(verbose.sys, 'stderr', err), \
             mock.patch.object(verbose.ctypes, 'windll') as windll, \
             mock.patch('builtins.open') as open_mock, \
             mock.patch.dict(os.environ, {}, clear=False):
            verbose.setup_console()
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
            open_mock.assert_not_called()
            windll.kernel32.AttachConsole.assert_not_called()
            windll.kernel32.AllocConsole.assert_not_called()

    def test_missing_stream_is_bound_to_the_console(self) -> None:
        err = _FakeStream()
        with mock.patch.object(verbose.sys, 'stdout', None), \
             mock.patch.object(verbose.sys, 'stderr', err), \
             mock.patch.object(verbose.ctypes, 'windll') as windll, \
             mock.patch('builtins.open') as open_mock, \
             mock.patch.dict(os.environ, {}, clear=False):
            windll.kernel32.AttachConsole.return_value = 1
            verbose.setup_console()
            # Only the missing stdout is rebound; the usable stderr is untouched.
            open_mock.assert_called_once_with('CONOUT$', 'w', encoding='utf-8', errors='backslashreplace')
            self.assertIsNot(sys.stdout, None)
            self.assertIs(sys.stderr, err)


if __name__ == '__main__':
    unittest.main()
