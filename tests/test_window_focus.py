"""Tests for the window selection logic (pure part of window focusing)."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from agent_monitor_for_claude.app import _MonitorApi
from agent_monitor_for_claude.window_focus import open_directory, select_terminal_window, select_window, vscode_session_url

# (hwnd, pid, title)
_WINDOWS = [
    (101, 500, 'app.py - oku3d-app - Visual Studio Code'),
    (102, 500, 'README.md - edge264 - Visual Studio Code'),
    (201, 600, 'Windows Terminal'),
    (301, 700, 'Program Manager'),
]

# Terminal-fallback fixtures: the claude session's tab is the active WT tab, so
# its window title carries the session title behind Claude Code's status glyph;
# an unrelated browser tab happens to repeat the same text.
_TERM_WINDOWS = [
    (900, 42, '✳ Implement AskUser dialog interaction'),
    (901, 42, 'Git CMD'),
    (902, 77, 'Implement AskUser dialog interaction - Google Chrome'),
]
_TERM_OWNERS = {42: 'windowsterminal.exe', 77: 'chrome.exe'}


class SelectWindowTest(unittest.TestCase):
    def test_prefers_title_matching_project(self) -> None:
        self.assertEqual(select_window(_WINDOWS, [500], 'edge264'), 102)

    def test_falls_back_to_first_window_without_title_match(self) -> None:
        self.assertEqual(select_window(_WINDOWS, [500], 'unrelated-project'), 101)

    def test_nearest_ancestor_with_windows_wins(self) -> None:
        self.assertEqual(select_window(_WINDOWS, [999, 600, 500], 'oku3d-app'), 201)

    def test_empty_project_name_uses_first_window(self) -> None:
        self.assertEqual(select_window(_WINDOWS, [500], ''), 101)

    def test_no_candidate_owns_a_window(self) -> None:
        self.assertIsNone(select_window(_WINDOWS, [111, 222], 'oku3d-app'))

    def test_case_insensitive_title_match(self) -> None:
        self.assertEqual(select_window(_WINDOWS, [500], 'EDGE264'), 102)


class SelectTerminalWindowTest(unittest.TestCase):
    def test_matches_terminal_window_by_session_title(self) -> None:
        self.assertEqual(select_terminal_window(_TERM_WINDOWS, _TERM_OWNERS, 'Implement AskUser dialog interaction'), 900)

    def test_ignores_non_terminal_owner_with_matching_title(self) -> None:
        # Only the browser window carries the title; its owner is not a terminal.
        windows = [(902, 77, 'Implement AskUser dialog interaction - Google Chrome')]
        self.assertIsNone(select_terminal_window(windows, _TERM_OWNERS, 'Implement AskUser dialog interaction'))

    def test_case_insensitive_match(self) -> None:
        self.assertEqual(select_terminal_window(_TERM_WINDOWS, _TERM_OWNERS, 'IMPLEMENT askuser DIALOG interaction'), 900)

    def test_empty_title_disables_match(self) -> None:
        self.assertIsNone(select_terminal_window(_TERM_WINDOWS, _TERM_OWNERS, ''))
        self.assertIsNone(select_terminal_window(_TERM_WINDOWS, _TERM_OWNERS, '   '))

    def test_too_short_title_disables_match(self) -> None:
        windows = [(910, 42, 'ab - Command Prompt')]
        self.assertIsNone(select_terminal_window(windows, _TERM_OWNERS, 'ab'))

    def test_no_matching_title_returns_none(self) -> None:
        self.assertIsNone(select_terminal_window(_TERM_WINDOWS, _TERM_OWNERS, 'Unrelated session'))


class VscodeSessionUrlTest(unittest.TestCase):
    def test_valid_uuid(self) -> None:
        url = vscode_session_url('A7A12D93-E700-4D96-B024-689A35C12BC2')
        self.assertEqual(url, 'vscode://anthropic.claude-code/open?session=a7a12d93-e700-4d96-b024-689a35c12bc2')

    def test_invalid_ids_are_rejected(self) -> None:
        self.assertIsNone(vscode_session_url(''))
        self.assertIsNone(vscode_session_url('not-a-uuid'))
        self.assertIsNone(vscode_session_url('a7a12d93-e700-4d96-b024-689a35c12bc2&prompt=evil'))
        self.assertIsNone(vscode_session_url('../escape'))


class OpenDirectoryTest(unittest.TestCase):
    """Only an existing directory may ever reach the shell."""

    def test_opens_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('agent_monitor_for_claude.window_focus.os.startfile') as startfile:
                self.assertTrue(open_directory(tmp))
                startfile.assert_called_once_with(tmp)

    def test_rejects_empty_path(self) -> None:
        with mock.patch('agent_monitor_for_claude.window_focus.os.startfile') as startfile:
            self.assertFalse(open_directory(''))
            startfile.assert_not_called()

    def test_rejects_missing_directory(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), 'amc-no-such-dir-4f2a9c')
        with mock.patch('agent_monitor_for_claude.window_focus.os.startfile') as startfile:
            self.assertFalse(open_directory(missing))
            startfile.assert_not_called()

    def test_rejects_a_file(self) -> None:
        # A file is not a directory - never hand an arbitrary (possibly
        # executable) file to the shell.
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, 'note.txt')
            with open(file_path, 'w', encoding='utf-8') as handle:
                handle.write('x')
            with mock.patch('agent_monitor_for_claude.window_focus.os.startfile') as startfile:
                self.assertFalse(open_directory(file_path))
                startfile.assert_not_called()

    def test_propagates_startfile_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('agent_monitor_for_claude.window_focus.os.startfile', side_effect=OSError):
                self.assertFalse(open_directory(tmp))


class OpenPathBridgeTest(unittest.TestCase):
    """The JS bridge must reject junk and only forward real strings on to the shell."""

    def test_rejects_non_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.open_directory') as opener:
            self.assertFalse(api.open_path(123))
            self.assertFalse(api.open_path(None))
            self.assertFalse(api.open_path(True))
            opener.assert_not_called()

    def test_rejects_empty_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.open_directory') as opener:
            self.assertFalse(api.open_path(''))
            opener.assert_not_called()

    def test_forwards_valid_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True) as opener:
            self.assertTrue(api.open_path('D:\\Projects\\aurora-realtime'))
            opener.assert_called_once_with('D:\\Projects\\aurora-realtime')


if __name__ == '__main__':
    unittest.main()
