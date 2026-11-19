"""
Tests for the window-focus module: the pure selection logic, and the guards that
stand in front of the platform's launch surfaces.

The selection fixtures use Windows process names because that is the busier of
the two host tables; the logic itself only compares strings, and the Linux name
set is covered in ``test_process_linux.py``.  The launch calls themselves are
the platform layer's (``test_platforms_win32.py`` / ``test_platforms_linux.py``);
what is pinned here is that nothing but a real directory or a real file ever
reaches them.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import window_focus
from agent_monitor_for_claude.app import _MonitorApi
from agent_monitor_for_claude.paths import SessionRoot, local_root
from agent_monitor_for_claude.platforms import IS_WINDOWS
from agent_monitor_for_claude.process_probe import ENTRYPOINT_WINDOW_OWNERS
from agent_monitor_for_claude.procinfo import EntrypointWindow
from agent_monitor_for_claude.window_focus import (
    focus_entrypoint_window, focus_terminal_window, open_directory, reveal_in_file_manager, select_entrypoint_window, select_terminal_window,
    select_window, vscode_session_url,
)

_WINDOWS_ONLY = unittest.skipUnless(IS_WINDOWS, 'Windows host path translation')

# A terminal emulator this system's process backend recognises, and an owner no
# backend does - the title match is confined to the former.
_TERMINAL_OWNER = 'windowsterminal.exe' if IS_WINDOWS else 'konsole'
_OTHER_OWNER = 'chrome.exe'

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
_TERM_OWNERS = {42: _TERMINAL_OWNER, 77: _OTHER_OWNER}


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
            with mock.patch.object(window_focus, 'open_path', return_value=True) as opener:
                self.assertTrue(open_directory(tmp))
                opener.assert_called_once_with(tmp)

    def test_rejects_empty_path(self) -> None:
        with mock.patch.object(window_focus, 'open_path') as opener:
            self.assertFalse(open_directory(''))
            opener.assert_not_called()

    def test_rejects_missing_directory(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), 'amc-no-such-dir-4f2a9c')
        with mock.patch.object(window_focus, 'open_path') as opener:
            self.assertFalse(open_directory(missing))
            opener.assert_not_called()

    def test_rejects_a_file(self) -> None:
        # A file is not a directory - never hand an arbitrary (possibly
        # executable) file to the shell.
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, 'note.txt')
            with open(file_path, 'w', encoding='utf-8') as handle:
                handle.write('x')
            with mock.patch.object(window_focus, 'open_path') as opener:
                self.assertFalse(open_directory(file_path))
                opener.assert_not_called()

    def test_propagates_a_refusing_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(window_focus, 'open_path', return_value=False):
                self.assertFalse(open_directory(tmp))


class RevealInExplorerTest(unittest.TestCase):
    """Only an existing file may ever reach the platform's show-in-folder call."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._file = os.path.join(self._dir.name, 'session.jsonl')
        with open(self._file, 'w', encoding='utf-8') as handle:
            handle.write('{}\n')

    def test_shows_existing_file_selected(self) -> None:
        with mock.patch.object(window_focus, 'reveal_file', return_value=True) as select, \
             mock.patch.object(window_focus, 'open_directory') as opener:
            self.assertTrue(reveal_in_file_manager(self._file))

        select.assert_called_once_with(self._file)
        # The file is selected in its folder, never opened - no fallback needed.
        opener.assert_not_called()

    def test_falls_back_to_containing_folder(self) -> None:
        with mock.patch.object(window_focus, 'reveal_file', return_value=False), \
             mock.patch.object(window_focus, 'open_directory', return_value=True) as opener:
            self.assertTrue(reveal_in_file_manager(self._file))

        opener.assert_called_once_with(self._dir.name)

    def test_rejects_empty_and_missing_path(self) -> None:
        missing = os.path.join(self._dir.name, 'gone.jsonl')
        with mock.patch.object(window_focus, 'reveal_file') as select, \
             mock.patch.object(window_focus, 'open_directory') as opener:
            self.assertFalse(reveal_in_file_manager(''))
            self.assertFalse(reveal_in_file_manager(missing))

        select.assert_not_called()
        opener.assert_not_called()

    def test_rejects_a_directory(self) -> None:
        # This surface is for a file; a directory belongs to open_directory, which
        # validates it in its own right.
        with mock.patch.object(window_focus, 'reveal_file') as select:
            self.assertFalse(reveal_in_file_manager(self._dir.name))

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
        # root_for_origin('local') is pinned to the real local_root(): open_path now
        # resolves origin before translating the path, so this must keep succeeding
        # regardless of whether the machine running the suite has WSL installed.
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=local_root()), \
             mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True) as opener:
            self.assertTrue(api.open_path('D:\\Projects\\aurora-realtime'))
            opener.assert_called_once_with('D:\\Projects\\aurora-realtime')


class OpenPathOriginTest(unittest.TestCase):
    """``open_path`` resolves *origin* to a root and translates a WSL-reported path through it."""

    @_WINDOWS_ONLY
    def test_wsl_origin_translates_to_unc_path(self) -> None:
        wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), claude_temp_dir=Path('tmp'))
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=wsl_root) as root_for_origin, \
             mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True) as opener:
            self.assertTrue(api.open_path('/home/dev/proj', origin='wsl:U'))

        root_for_origin.assert_called_once_with('wsl:U')
        opener.assert_called_once_with(r'\\wsl.localhost\U\home\dev\proj')

    def test_the_resolved_origin_is_still_required_on_every_host(self) -> None:
        # A WSL root can only exist on Windows, but resolving the origin first is
        # what refuses a stale one, and that must hold on both systems.
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=local_root()) as root_for_origin, \
             mock.patch('agent_monitor_for_claude.app.open_directory', return_value=True):
            self.assertTrue(api.open_path('/home/dev/proj', origin='local'))

        root_for_origin.assert_called_once_with('local')

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
        with mock.patch('agent_monitor_for_claude.app.reveal_in_file_manager') as reveal:
            self.assertFalse(api.reveal_path(123))
            self.assertFalse(api.reveal_path(None))
            self.assertFalse(api.reveal_path(True))
            self.assertFalse(api.reveal_path(''))
            reveal.assert_not_called()

    def test_forwards_valid_path(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=local_root()), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_file_manager', return_value=True) as reveal:
            self.assertTrue(api.reveal_path('D:\\Projects\\aurora-realtime\\a1d.jsonl'))

        reveal.assert_called_once_with('D:\\Projects\\aurora-realtime\\a1d.jsonl')

    @_WINDOWS_ONLY
    def test_wsl_origin_translates_to_unc_path(self) -> None:
        wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), claude_temp_dir=Path('tmp'))
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=wsl_root), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_file_manager', return_value=True) as reveal:
            self.assertTrue(api.reveal_path('/home/dev/.claude/projects/-home-dev-proj/a1d.jsonl', origin='wsl:U'))

        reveal.assert_called_once_with(r'\\wsl.localhost\U\home\dev\.claude\projects\-home-dev-proj\a1d.jsonl')

    def test_refused_origins_never_reach_the_shell(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app.root_for_origin', return_value=None), \
             mock.patch('agent_monitor_for_claude.app.reveal_in_file_manager') as reveal:
            self.assertFalse(api.reveal_path('/home/dev/a1d.jsonl', origin='gone:X'))
            self.assertFalse(api.reveal_path('D:\\Projects\\a1d.jsonl', origin=123))

        reveal.assert_not_called()


class FocusTerminalWindowTest(unittest.TestCase):
    """``focus_terminal_window`` is the pid-free route ``focus_session`` uses for WSL sessions.

    A session running inside a WSL distribution has no host-side process at all, so there is no pid
    to walk an ancestor chain from - the only way to find its terminal is the same title match
    ``focus_session_window`` already falls back to for a local external-terminal session.
    """

    def test_finds_and_activates_terminal_window_by_title(self) -> None:
        windows = [(900, 42, '✳ Implement AskUser dialog interaction')]
        with mock.patch.object(window_focus, 'enum_windows', return_value=windows), \
             mock.patch.object(window_focus, 'process_names', return_value={42: _TERMINAL_OWNER}), \
             mock.patch.object(window_focus, 'activate_window', return_value=True) as activate:
            self.assertTrue(focus_terminal_window('Implement AskUser dialog interaction'))

        activate.assert_called_once_with(900)

    def test_returns_false_when_no_title_match(self) -> None:
        windows = [(900, 42, 'Unrelated window title')]
        with mock.patch.object(window_focus, 'enum_windows', return_value=windows), \
             mock.patch.object(window_focus, 'process_names', return_value={42: _TERMINAL_OWNER}), \
             mock.patch.object(window_focus, 'activate_window') as activate:
            self.assertFalse(focus_terminal_window('Implement AskUser dialog interaction'))

        activate.assert_not_called()


# A table shaped like the platform's, plus a multi-window editor entry that picks by project.
_ENTRYPOINT_RULES = {
    'claude-desktop': EntrypointWindow(owners=frozenset({'claude.exe'}), title='Claude'),
    'claude-vscode': EntrypointWindow(owners=frozenset({'code.exe', 'code - insiders.exe', 'cursor.exe'}), match_project=True),
}


class SelectEntrypointWindowTest(unittest.TestCase):
    """The owner-based choice for a WSL session whose application has a window of its own on this side."""

    def test_picks_the_owners_window_with_the_wanted_title(self) -> None:
        windows = [(10, 1, 'Windows Terminal'), (20, 2, 'Claude')]
        owners = {1: 'windowsterminal.exe', 2: 'claude.exe'}

        self.assertEqual(select_entrypoint_window(windows, owners, 'claude-desktop', 'proj', _ENTRYPOINT_RULES), 20)

    def test_unknown_or_missing_entrypoint_selects_nothing(self) -> None:
        windows = [(20, 2, 'Claude')]
        owners = {2: 'claude.exe'}

        for entrypoint in ('', 'cli', 'constructor', None, 42):
            with self.subTest(entrypoint=entrypoint):
                self.assertIsNone(select_entrypoint_window(windows, owners, entrypoint, 'proj', _ENTRYPOINT_RULES))  # type: ignore[arg-type]  # untrusted value

    def test_no_matching_window_selects_nothing(self) -> None:
        windows = [(10, 1, 'Windows Terminal'), (30, 3, 'Claude')]
        owners = {1: 'windowsterminal.exe', 3: 'chrome.exe'}

        self.assertIsNone(select_entrypoint_window(windows, owners, 'claude-desktop', 'proj', _ENTRYPOINT_RULES))
        self.assertIsNone(select_entrypoint_window([], {}, 'claude-desktop', 'proj', _ENTRYPOINT_RULES))

    def test_a_claude_exe_window_with_another_title_is_never_picked(self) -> None:
        # The native CLI is claude.exe too; only the Desktop app's main window qualifies.
        windows = [(40, 4, '\u2733 Fix the login flow'), (41, 4, 'Claude Code'), (42, 4, '')]
        owners = {4: 'claude.exe'}

        self.assertIsNone(select_entrypoint_window(windows, owners, 'claude-desktop', 'proj', _ENTRYPOINT_RULES))

    def test_a_windowless_cli_process_is_never_picked(self) -> None:
        # The CLI's console window belongs to its console host, never to claude.exe itself.
        windows = [(50, 5, 'Claude')]
        owners = {5: 'conhost.exe', 6: 'claude.exe'}

        self.assertIsNone(select_entrypoint_window(windows, owners, 'claude-desktop', 'proj', _ENTRYPOINT_RULES))

    def test_several_owners_prefer_the_project_window(self) -> None:
        windows = [(60, 6, 'a.py - other - Visual Studio Code'), (61, 7, 'b.py - proj - Cursor'), (62, 6, 'c.py - proj - Visual Studio Code')]
        owners = {6: 'code.exe', 7: 'cursor.exe'}

        self.assertEqual(select_entrypoint_window(windows, owners, 'claude-vscode', 'proj', _ENTRYPOINT_RULES), 61)
        self.assertEqual(select_entrypoint_window(windows, owners, 'claude-vscode', 'none', _ENTRYPOINT_RULES), 60)
        self.assertEqual(select_entrypoint_window(windows, owners, 'claude-vscode', '', _ENTRYPOINT_RULES), 60)


class EntrypointWindowTableTest(unittest.TestCase):
    """The platform's own table: Claude Desktop on Windows, nothing on Linux."""

    @unittest.skipUnless(IS_WINDOWS, 'the Windows table')
    def test_windows_table_finds_only_the_desktop_main_window(self) -> None:
        windows = [(70, 8, 'Claude'), (71, 9, 'Claude')]
        owners = {8: 'claude.exe', 9: 'notepad.exe'}

        self.assertEqual(select_entrypoint_window(windows, owners, 'claude-desktop', '', ENTRYPOINT_WINDOW_OWNERS), 70)
        self.assertIsNone(select_entrypoint_window(windows, owners, 'cli', '', ENTRYPOINT_WINDOW_OWNERS))

    @unittest.skipIf(IS_WINDOWS, 'the Linux table')
    def test_linux_table_is_empty(self) -> None:
        self.assertEqual(ENTRYPOINT_WINDOW_OWNERS, {})


class FocusEntrypointWindowTest(unittest.TestCase):
    def test_activates_the_selected_window(self) -> None:
        rules = {'claude-desktop': EntrypointWindow(owners=frozenset({'claude.exe'}), title='Claude')}
        with mock.patch.object(window_focus, 'ENTRYPOINT_WINDOW_OWNERS', rules), \
             mock.patch.object(window_focus, 'enum_windows', return_value=[(900, 42, 'Claude')]), \
             mock.patch.object(window_focus, 'process_names', return_value={42: 'claude.exe'}), \
             mock.patch.object(window_focus, 'activate_window', return_value=True) as activate:
            self.assertTrue(focus_entrypoint_window('claude-desktop'))

        activate.assert_called_once_with(900)

    def test_unlisted_entrypoint_enumerates_nothing(self) -> None:
        with mock.patch.object(window_focus, 'enum_windows') as enum, \
             mock.patch.object(window_focus, 'activate_window') as activate:
            self.assertFalse(focus_entrypoint_window('cli'))
            self.assertFalse(focus_entrypoint_window(''))

        enum.assert_not_called()
        activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
