"""
Tests for the shared verbose-diagnostics frame.

The per-system rows and the console attachment are the platform backends' own,
covered in ``test_platforms_win32.py`` / ``test_platforms_linux.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import verbose

_WINDOWS_ONLY = unittest.skipUnless(sys.platform == 'win32', 'Windows path semantics')
_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'POSIX path semantics')


def _redact(path_str: str, home: str) -> str:
    """Redact *path_str* against a pretended home directory."""
    with mock.patch('agent_monitor_for_claude.verbose.Path.home', return_value=Path(home)):
        return verbose._redact_home(path_str)


@_WINDOWS_ONLY
class RedactWindowsHomeTest(unittest.TestCase):
    """Windows paths compare case- and separator-insensitively, which only holds there."""

    _HOME = 'C:\\Users\\jens'

    def test_redacts_exact_home_and_a_prefix(self) -> None:
        self.assertEqual(_redact('C:\\Users\\jens', self._HOME), '~')
        self.assertEqual(_redact('C:\\Users\\jens\\.claude', self._HOME), '~\\.claude')

    def test_redacts_case_insensitively(self) -> None:
        # A hand-typed lower-case path must still be redacted on case-insensitive
        # NTFS - otherwise the full home path (and the username) is printed.
        self.assertEqual(_redact('c:\\users\\jens\\.claude', self._HOME), '~\\.claude')

    def test_does_not_over_match_a_sibling_directory(self) -> None:
        # 'jens2' merely starts with 'jens'; it must not be redacted to '~2'.
        self.assertEqual(_redact('C:\\Users\\jens2\\project', self._HOME), 'C:\\Users\\jens2\\project')

    def test_leaves_an_unrelated_path_untouched(self) -> None:
        self.assertEqual(_redact('D:\\Work\\proj', self._HOME), 'D:\\Work\\proj')


@_LINUX_ONLY
class RedactPosixHomeTest(unittest.TestCase):
    """A Linux path is compared as typed - two names differing only in case are two directories."""

    _HOME = '/home/dev'

    def test_redacts_exact_home_and_a_prefix(self) -> None:
        self.assertEqual(_redact('/home/dev', self._HOME), '~')
        self.assertEqual(_redact('/home/dev/.claude', self._HOME), '~/.claude')

    def test_does_not_over_match_a_sibling_directory(self) -> None:
        self.assertEqual(_redact('/home/dev2/project', self._HOME), '/home/dev2/project')

    def test_leaves_an_unrelated_path_untouched(self) -> None:
        self.assertEqual(_redact('/opt/work/proj', self._HOME), '/opt/work/proj')


class SetupConsoleTest(unittest.TestCase):
    def test_raises_pywebviews_log_level_on_both_systems(self) -> None:
        # Attaching a console is the platform's job; the log level is not, so it
        # is set here whether or not a console was needed.
        with mock.patch.object(verbose, '_platform_setup_console') as attach, \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PYWEBVIEW_LOG', None)
            verbose.setup_console()
            attach.assert_called_once_with()
            self.assertEqual(os.environ['PYWEBVIEW_LOG'], 'DEBUG')


if __name__ == '__main__':
    unittest.main()
