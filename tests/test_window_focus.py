"""Tests for the window selection logic (pure part of window focusing)."""
from __future__ import annotations

import unittest

from agent_monitor_for_claude.window_focus import select_terminal_window, select_window, vscode_session_url

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


if __name__ == '__main__':
    unittest.main()
