"""Tests for the window selection logic (pure part of window focusing)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import window_focus
from agent_monitor_for_claude.app import _MonitorApi
from agent_monitor_for_claude.paths import SessionRoot, windows_root
from agent_monitor_for_claude.window_focus import (
    focus_terminal_window, open_directory, reveal_in_explorer, select_terminal_window, select_window, vscode_session_url,
)

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


class RevealInExplorerTest(unittest.TestCase):
    """Only an existing file may ever reach the shell's select-in-folder call."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._file = os.path.join(self._dir.name, 'session.jsonl')
        with open(self._file, 'w', encoding='utf-8') as handle:
            handle.write('{}\n')

    def test_shows_existing_file_selected(self) -> None:
        with mock.patch.object(window_focus, '_select_in_explorer', return_value=True) as select, \
             mock.patch.object(window_focus, 'open_directory') as opener:
            self.assertTrue(reveal_in_explorer(self._file))

        select.assert_called_once_with(self._file)
        # The file is selected in its folder, never opened - no fallback needed.
        opener.assert_not_called()

    def test_falls_back_to_containing_folder(self) -> None:
        with mock.patch.object(window_focus, '_select_in_explorer', return_value=False), \
             mock.patch.object(window_focus, 'open_directory', return_value=True) as opener:
            self.assertTrue(reveal_in_explorer(self._file))

        opener.assert_called_once_with(self._dir.name)

    def test_rejects_empty_and_missing_path(self) -> None:
        missing = os.path.join(self._dir.name, 'gone.jsonl')
        with mock.patch.object(window_focus, '_select_in_explorer') as select, \
             mock.patch.object(window_focus, 'open_directory') as opener:
            self.assertFalse(reveal_in_explorer(''))
            self.assertFalse(reveal_in_explorer(missing))

        select.assert_not_called()
        opener.assert_not_called()

    def test_rejects_a_directory(self) -> None:
        # This surface is for a file; a directory belongs to open_directory, which
        # validates it in its own right.
        with mock.patch.object(window_focus, '_select_in_explorer') as select:
            self.assertFalse(reveal_in_explorer(self._dir.name))

        select.assert_not_called()


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
        # root_for_origin('windows') is pinned to the real windows_root(): open_path now
        # resolves origin before translating the path, so this must keep succeeding
        # regardless of whether the machine running the suite has WSL installed.
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=windows_root()), \
             mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True) as opener:
            self.assertTrue(api.open_path('D:\\Projects\\aurora-realtime'))
            opener.assert_called_once_with('D:\\Projects\\aurora-realtime')


class OpenPathOriginTest(unittest.TestCase):
    """``open_path`` resolves *origin* to a root and translates a WSL-reported path through it."""

    def test_wsl_origin_translates_to_unc_path(self) -> None:
        wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), temp_dir=Path('tmp'))
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=wsl_root) as root_for_origin, \
             mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True) as opener:
            self.assertTrue(api.open_path('/home/dev/proj', origin='wsl:U'))

        root_for_origin.assert_called_once_with('wsl:U')
        opener.assert_called_once_with(r'\\wsl.localhost\U\home\dev\proj')

    def test_unknown_origin_is_refused_without_calling_open_directory(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=None), \
             mock.patch('agent_monitor_for_claude.app.open_directory') as opener:
            self.assertFalse(api.open_path('/home/dev/proj', origin='gone:X'))

        opener.assert_not_called()

    def test_non_str_origin_is_refused_without_calling_open_directory(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.open_directory') as opener:
            self.assertFalse(api.open_path('D:\\Projects\\aurora-realtime', origin=123))

        opener.assert_not_called()


class RevealPathBridgeTest(unittest.TestCase):
    """``reveal_path`` is ``open_path`` for a file: junk is refused, a WSL path translated first."""

    def test_rejects_non_string_and_empty(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.reveal_in_explorer') as reveal:
            self.assertFalse(api.reveal_path(123))
            self.assertFalse(api.reveal_path(None))
            self.assertFalse(api.reveal_path(True))
            self.assertFalse(api.reveal_path(''))
            reveal.assert_not_called()

    def test_forwards_valid_path(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=windows_root()), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_explorer', return_value=True) as reveal:
            self.assertTrue(api.reveal_path('D:\\Projects\\aurora-realtime\\a1d.jsonl'))

        reveal.assert_called_once_with('D:\\Projects\\aurora-realtime\\a1d.jsonl')

    def test_wsl_origin_translates_to_unc_path(self) -> None:
        wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), temp_dir=Path('tmp'))
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=wsl_root), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_explorer', return_value=True) as reveal:
            self.assertTrue(api.reveal_path('/home/dev/.claude/projects/-home-dev-proj/a1d.jsonl', origin='wsl:U'))

        reveal.assert_called_once_with(r'\\wsl.localhost\U\home\dev\.claude\projects\-home-dev-proj\a1d.jsonl')

    def test_refused_origins_never_reach_the_shell(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=None), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_explorer') as reveal:
            self.assertFalse(api.reveal_path('/home/dev/a1d.jsonl', origin='gone:X'))
            self.assertFalse(api.reveal_path('D:\\Projects\\a1d.jsonl', origin=123))

        reveal.assert_not_called()


class FocusTerminalWindowTest(unittest.TestCase):
    """``focus_terminal_window`` is the pid-free route ``focus_session`` uses for WSL sessions.

    A session running inside a WSL distribution has no Windows process at all, so there is no pid to
    walk an ancestor chain from - the only way to find its terminal is the same title match
    ``focus_session_window`` already falls back to for a native external-terminal session.
    """

    def test_finds_and_activates_terminal_window_by_title(self) -> None:
        windows = [(900, 42, '✳ Implement AskUser dialog interaction')]
        with mock.patch.object(window_focus, '_enum_windows', return_value=windows), \
             mock.patch.object(window_focus, 'process_names', return_value={42: 'windowsterminal.exe'}), \
             mock.patch.object(window_focus, '_activate', return_value=True) as activate:
            self.assertTrue(focus_terminal_window('Implement AskUser dialog interaction'))

        activate.assert_called_once_with(900)

    def test_returns_false_when_no_title_match(self) -> None:
        windows = [(900, 42, 'Unrelated window title')]
        with mock.patch.object(window_focus, '_enum_windows', return_value=windows), \
             mock.patch.object(window_focus, 'process_names', return_value={42: 'windowsterminal.exe'}), \
             mock.patch.object(window_focus, '_activate') as activate:
            self.assertFalse(focus_terminal_window('Implement AskUser dialog interaction'))

        activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
