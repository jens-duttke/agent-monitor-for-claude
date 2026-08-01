"""Tests for the _MonitorApi bridge behavior."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import app
from agent_monitor_for_claude.app import _LOG_MAX_LEN, _MonitorApi, _sanitize_log
from agent_monitor_for_claude.paths import SessionRoot, cwd_to_slug, windows_root
from agent_monitor_for_claude.process_probe import ChildProcessStat
from agent_monitor_for_claude.tasks import TaskInfo


class SanitizeLogTest(unittest.TestCase):
    def test_escapes_control_and_surrogate_chars(self) -> None:
        self.assertEqual(_sanitize_log('hello'), 'hello')
        self.assertEqual(_sanitize_log('a\tb\nc'), 'a\tb\nc')   # tab/newline are kept
        self.assertNotIn('\x1b', _sanitize_log('\x1b[2Jx'))     # ESC (ANSI/OSC) escaped
        self.assertNotIn('\x07', _sanitize_log('\x07'))         # BEL escaped
        self.assertNotIn('\x7f', _sanitize_log('\x7f'))         # DEL escaped
        self.assertNotIn('\ud800', _sanitize_log('x\ud800y'))   # lone surrogate escaped
        self.assertIn('emoji \U0001f600', _sanitize_log('emoji \U0001f600'))  # real high chars kept

    def test_caps_length(self) -> None:
        out = _sanitize_log('a' * (_LOG_MAX_LEN + 500))
        self.assertTrue(out.endswith('...'))
        self.assertLessEqual(len(out), _LOG_MAX_LEN + 3)


class LogSanitizeTest(unittest.TestCase):
    def test_log_strips_control_chars_from_output(self) -> None:
        api = _MonitorApi()
        buf = io.StringIO()
        with mock.patch.object(app.sys, 'stderr', buf):
            api.log('\x1b[2Jhi')
        out = buf.getvalue()
        self.assertNotIn('\x1b', out)
        self.assertIn('hi', out)


class ScratchpadPathTest(unittest.TestCase):
    _SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'
    _CWD = r'D:\WebDev\vs-edge264'

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        patcher = mock.patch('tempfile.gettempdir', return_value=self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._scratch = Path(self._tmp) / 'claude' / cwd_to_slug(self._CWD) / self._SESSION / 'scratchpad'

        # Pin origin resolution to this fixture's own windows_root() (built after the
        # tempdir patch above, so its temp_dir matches self._scratch) - without this,
        # scratchpad_path's now-real root_for_origin('windows') call would reach the
        # real roots.session_roots() and thus real WSL discovery on every test here.
        self._windows_root = windows_root()
        root_patcher = mock.patch.object(app, 'root_for_origin', side_effect=lambda origin: self._windows_root if origin == 'windows' else None)
        root_patcher.start()
        self.addCleanup(root_patcher.stop)

    def test_returns_path_only_when_directory_exists(self) -> None:
        api = _MonitorApi()
        self.assertEqual(api.scratchpad_path(self._SESSION, self._CWD), '')

        self._scratch.mkdir(parents=True)
        self.assertEqual(api.scratchpad_path(self._SESSION, self._CWD), str(self._scratch))

    def test_rejects_bad_input(self) -> None:
        api = _MonitorApi()
        self.assertEqual(api.scratchpad_path('', self._CWD), '')
        self.assertEqual(api.scratchpad_path(self._SESSION, ''), '')
        self.assertEqual(api.scratchpad_path(None, None), '')
        # A non-UUID session id (e.g. a traversal attempt) is refused before it
        # is ever built into a path.
        self.assertEqual(api.scratchpad_path('..\\..\\Windows', self._CWD), '')
        self.assertEqual(api.scratchpad_path('not-a-uuid', self._CWD), '')


class RunSearchFailureTest(unittest.TestCase):
    """An unexpected backend failure must not read as a successful empty search."""

    def test_backend_failure_is_reported_as_error_not_empty(self) -> None:
        api = _MonitorApi()
        api._search_seq = 7
        pushes: list[tuple] = []
        api._push_search = lambda *args: pushes.append(args)  # type: ignore[method-assign]

        with mock.patch('agent_monitor_for_claude.app.run_search', side_effect=RuntimeError('boom')):
            api._run_search('query', [], {}, 7)

        self.assertEqual(len(pushes), 1)
        _seq, _processed, _total, _matches, done, error = pushes[0]
        self.assertTrue(done, 'the progress state must still be cleared')
        self.assertTrue(error, 'a backend failure must be reported as an error, not a clean "no matches" result')

    def test_superseded_failure_pushes_nothing(self) -> None:
        # If the search was superseded, a late failure must stay silent.
        api = _MonitorApi()
        api._search_seq = 9  # newer than the failing search's seq
        pushes: list[tuple] = []
        api._push_search = lambda *args: pushes.append(args)  # type: ignore[method-assign]

        with mock.patch('agent_monitor_for_claude.app.run_search', side_effect=RuntimeError('boom')):
            api._run_search('query', [], {}, 7)

        self.assertEqual(pushes, [])


class FocusSessionPidTest(unittest.TestCase):
    def test_non_finite_pid_returns_false_without_raising(self) -> None:
        # int(float('inf')) raises OverflowError (not caught by TypeError/ValueError);
        # a non-finite pid must degrade to a graceful False, never propagate.
        api = _MonitorApi()
        self.assertFalse(api.focus_session(float('inf')))
        self.assertFalse(api.focus_session(float('-inf')))
        self.assertFalse(api.focus_session(float('nan')))


class FocusSessionOriginTest(unittest.TestCase):
    """A WSL-origin session has no Windows process, so focusing it must never touch pid-based Win32 APIs.

    A Windows pid can be recycled for an unrelated process, so handing a WSL session's Linux pid to
    ``focus_session_window`` (which walks the ancestor chain and calls Win32 window APIs) could raise
    the wrong window entirely.  For a ``wsl:`` origin the pid must never even be inspected - the
    session's terminal is found purely by title, via ``focus_terminal_window``.
    """

    def test_wsl_origin_uses_terminal_title_route_only(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'focus_session_window') as focus_window, \
             mock.patch.object(app, 'focus_terminal_window', return_value=True) as focus_terminal:
            result = api.focus_session(12345, session_title='My Session', origin='wsl:Ubuntu')

        self.assertTrue(result)
        focus_window.assert_not_called()
        focus_terminal.assert_called_once_with('My Session')

    def test_wsl_origin_ignores_an_invalid_pid(self) -> None:
        # pid validation must be skipped entirely for a WSL origin: a value that would raise on
        # int() must not stop the title-based route, since the pid is never used for it.
        api = _MonitorApi()
        with mock.patch.object(app, 'focus_session_window') as focus_window, \
             mock.patch.object(app, 'focus_terminal_window', return_value=False) as focus_terminal:
            result = api.focus_session(float('nan'), session_title='Some Title', origin='wsl:Ubuntu')

        self.assertFalse(result)
        focus_window.assert_not_called()
        focus_terminal.assert_called_once_with('Some Title')

    def test_windows_origin_never_calls_focus_terminal_window(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'focus_session_window', return_value=True) as focus_window, \
             mock.patch.object(app, 'focus_terminal_window') as focus_terminal:
            result = api.focus_session(4242, project_name='proj', session_title='title')

        self.assertTrue(result)
        focus_window.assert_called_once_with(4242, 'proj', 'title')
        focus_terminal.assert_not_called()

    def test_wsl_origin_still_combines_with_vscode_deeplink(self) -> None:
        # for WSL, vscode_deeplink will be False from the registry data we have, but the code path
        # stays consistent with the native route: raise the terminal, then try the deep link too.
        api = _MonitorApi()
        with mock.patch.object(app, 'focus_session_window') as focus_window, \
             mock.patch.object(app, 'focus_terminal_window', return_value=True), \
             mock.patch.object(app, 'open_vscode_session', return_value=True) as open_vscode, \
             mock.patch.object(app.time, 'sleep') as sleep_mock:
            result = api.focus_session(
                1, session_id='a7a12d93-e700-4d96-b024-689a35c12bc2', vscode_deeplink=True,
                session_title='title', origin='wsl:Ubuntu',
            )

        self.assertTrue(result)
        focus_window.assert_not_called()
        sleep_mock.assert_called_once()
        open_vscode.assert_called_once_with('a7a12d93-e700-4d96-b024-689a35c12bc2')


class DeleteSessionOriginTest(unittest.TestCase):
    def test_passes_origin_through(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, '_delete_session', return_value=True) as delete_mock:
            result = api.delete_session('6e22e66f-6298-442a-9762-2a5b65052389', r'D:\proj', 'wsl:U')

        self.assertTrue(result)
        delete_mock.assert_called_once_with('6e22e66f-6298-442a-9762-2a5b65052389', r'D:\proj', 'wsl:U')

    def test_default_origin_is_windows(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, '_delete_session', return_value=True) as delete_mock:
            api.delete_session('6e22e66f-6298-442a-9762-2a5b65052389', r'D:\proj')

        delete_mock.assert_called_once_with('6e22e66f-6298-442a-9762-2a5b65052389', r'D:\proj', 'windows')

    def test_non_str_origin_is_refused_without_calling_delete(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, '_delete_session') as delete_mock:
            result = api.delete_session('6e22e66f-6298-442a-9762-2a5b65052389', r'D:\proj', origin=123)

        self.assertFalse(result)
        delete_mock.assert_not_called()


class UnknownOriginRefusalTest(unittest.TestCase):
    """An origin naming no currently available root is a refusal, matching each method's own empty shape."""

    _SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'
    _CWD = r'D:\proj'

    def test_get_tasks_refuses(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'root_for_origin', return_value=None), mock.patch.object(app, 'list_tasks') as list_tasks_mock:
            result = api.get_tasks(self._SESSION, self._CWD, origin='gone:X')

        self.assertEqual(result, {'tasks': [], 'total': 0})
        list_tasks_mock.assert_not_called()

    def test_read_task_output_refuses(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'root_for_origin', return_value=None), mock.patch.object(app, '_read_task_output') as read_mock:
            result = api.read_task_output(self._SESSION, self._CWD, 'task1', origin='gone:X')

        self.assertIsNone(result)
        read_mock.assert_not_called()

    def test_get_process_stats_refuses(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'root_for_origin', return_value=None), mock.patch.object(app, 'process_stats') as stats_mock:
            result = api.get_process_stats(4242, origin='gone:X')

        self.assertEqual(result, [])
        stats_mock.assert_not_called()

    def test_scratchpad_path_refuses(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'root_for_origin', return_value=None):
            result = api.scratchpad_path(self._SESSION, self._CWD, origin='gone:X')

        self.assertEqual(result, '')

    def test_non_str_origin_refuses_the_same_way(self) -> None:
        api = _MonitorApi()
        self.assertEqual(api.get_tasks(self._SESSION, self._CWD, origin=123), {'tasks': [], 'total': 0})
        self.assertIsNone(api.read_task_output(self._SESSION, self._CWD, 'task1', origin=123))
        self.assertEqual(api.get_process_stats(4242, origin=123), [])
        self.assertEqual(api.scratchpad_path(self._SESSION, self._CWD, origin=123), '')


class WslOriginTasksTest(unittest.TestCase):
    """As of Task 14, a WSL origin's task queries route through to ``tasks`` with the resolved root.

    ``tasks.py`` is now per-root, so a WSL origin's background tasks are read from that root's own
    UNC tree exactly like a Windows session's - see ``tests/test_tasks.py`` for the root-aware
    confinement coverage. This mirrors WslOriginProcessStatsTest below, which made the same change
    for process stats back in Task 13.
    """

    _SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'
    _CWD = '/home/dev/proj'

    def _wsl_root(self) -> SessionRoot:
        return SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), temp_dir=Path('tmp'))

    def test_get_tasks_calls_list_tasks_with_the_resolved_root(self) -> None:
        api = _MonitorApi()
        wsl_root = self._wsl_root()
        info = TaskInfo(task_id='t1', size_bytes=10, age_seconds=1.5, label='build')
        with mock.patch.object(app, 'root_for_origin', return_value=wsl_root), \
             mock.patch.object(app, 'list_tasks', return_value=([info], 1)) as list_tasks_mock:
            result = api.get_tasks(self._SESSION, self._CWD, origin='wsl:U')

        self.assertEqual(result, {'tasks': [{'id': 't1', 'size': 10, 'age': 1.5, 'label': 'build'}], 'total': 1})
        list_tasks_mock.assert_called_once_with(wsl_root, self._SESSION, self._CWD, recent_seconds=None)

    def test_read_task_output_calls_read_task_output_with_the_resolved_root(self) -> None:
        api = _MonitorApi()
        wsl_root = self._wsl_root()
        with mock.patch.object(app, 'root_for_origin', return_value=wsl_root), \
             mock.patch.object(app, '_read_task_output', return_value='hello') as read_mock:
            result = api.read_task_output(self._SESSION, self._CWD, 'task1', origin='wsl:U')

        self.assertEqual(result, 'hello')
        read_mock.assert_called_once_with(wsl_root, self._SESSION, self._CWD, 'task1')


class WslOriginProcessStatsTest(unittest.TestCase):
    """As of Task 13, a WSL origin's get_process_stats routes to wsl_process_stats instead of degrading."""

    def _wsl_root(self) -> SessionRoot:
        return SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), temp_dir=Path('tmp'))

    def test_get_process_stats_calls_wsl_process_stats_with_registry_ticks(self) -> None:
        api = _MonitorApi()
        wsl_root = self._wsl_root()
        stat = ChildProcessStat(pid=99, name='node', cpu_percent=1.0, rss_bytes=2048, uptime_seconds=5.0)
        with mock.patch.object(app, 'root_for_origin', return_value=wsl_root), \
             mock.patch.object(app, 'list_sessions', return_value=[{'pid': 4242, 'proc_start_ticks': 5000}]) as list_sessions_mock, \
             mock.patch.object(app, 'wsl_process_stats', return_value=[stat]) as wsl_stats_mock:
            result = api.get_process_stats(4242, origin='wsl:U')

        self.assertEqual(result, [{'pid': 99, 'name': 'node', 'cpu': 1.0, 'rss': 2048, 'uptime': 5.0, 'kind': 'process'}])
        list_sessions_mock.assert_called_once_with(wsl_root)
        wsl_stats_mock.assert_called_once_with(wsl_root, 4242, 5000)

    def test_get_process_stats_ignores_a_registry_record_for_a_different_pid(self) -> None:
        api = _MonitorApi()
        wsl_root = self._wsl_root()
        with mock.patch.object(app, 'root_for_origin', return_value=wsl_root), \
             mock.patch.object(app, 'list_sessions', return_value=[{'pid': 1, 'proc_start_ticks': 999}]), \
             mock.patch.object(app, 'wsl_process_stats', return_value=[]) as wsl_stats_mock:
            api.get_process_stats(4242, origin='wsl:U')

        wsl_stats_mock.assert_called_once_with(wsl_root, 4242, None)


class WindowsOriginPassthroughTest(unittest.TestCase):
    """The default 'windows' origin must still resolve and call through, exactly as before Task 10."""

    _SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'
    _CWD = r'D:\proj'

    def _windows_root(self) -> SessionRoot:
        return SessionRoot(origin='windows', label=None, config_dir=Path('cfg'), proc_dir=None, temp_dir=Path('tmp'))

    def test_get_tasks_calls_list_tasks(self) -> None:
        api = _MonitorApi()
        windows_root_value = self._windows_root()
        info = TaskInfo(task_id='t1', size_bytes=10, age_seconds=1.5, label='build')
        with mock.patch.object(app, 'root_for_origin', return_value=windows_root_value), \
             mock.patch.object(app, 'list_tasks', return_value=([info], 1)) as list_tasks_mock:
            result = api.get_tasks(self._SESSION, self._CWD)

        self.assertEqual(result, {'tasks': [{'id': 't1', 'size': 10, 'age': 1.5, 'label': 'build'}], 'total': 1})
        list_tasks_mock.assert_called_once_with(windows_root_value, self._SESSION, self._CWD, recent_seconds=None)

    def test_read_task_output_calls_read_task_output(self) -> None:
        api = _MonitorApi()
        windows_root_value = self._windows_root()
        with mock.patch.object(app, 'root_for_origin', return_value=windows_root_value), \
             mock.patch.object(app, '_read_task_output', return_value='hello') as read_mock:
            result = api.read_task_output(self._SESSION, self._CWD, 'task1')

        self.assertEqual(result, 'hello')
        read_mock.assert_called_once_with(windows_root_value, self._SESSION, self._CWD, 'task1')

    def test_get_process_stats_uses_windows_root_for_the_registry_lookup(self) -> None:
        api = _MonitorApi()
        stat = ChildProcessStat(pid=99, name='node.exe', cpu_percent=1.0, rss_bytes=2048, uptime_seconds=5.0)
        with mock.patch.object(app, 'root_for_origin', return_value=self._windows_root()), \
             mock.patch.object(app, 'list_sessions', return_value=[{'pid': 4242, 'proc_start_ticks': 999}]) as list_sessions_mock, \
             mock.patch.object(app, 'process_stats', return_value=[stat]) as process_stats_mock:
            result = api.get_process_stats(4242)

        self.assertEqual(result, [{'pid': 99, 'name': 'node.exe', 'cpu': 1.0, 'rss': 2048, 'uptime': 5.0, 'kind': 'process'}])
        # The registry lookup always uses the standalone windows_root(), independent of the
        # resolved origin root object - its .origin must be the real 'windows' constant.
        self.assertEqual(list_sessions_mock.call_args[0][0].origin, 'windows')
        process_stats_mock.assert_called_once_with(4242, 999)


class StartSearchSeqTest(unittest.TestCase):
    """The active search seq must not regress, and must reset on a fresh page."""

    def _api(self) -> _MonitorApi:
        api = _MonitorApi()
        api._run_search = lambda *a, **k: None  # type: ignore[method-assign]  # keep the worker a no-op
        return api

    def test_reordered_start_does_not_regress_the_active_seq(self) -> None:
        # pywebview may deliver a later start_search on an earlier worker thread,
        # so a stale, lower seq must not overwrite the active, higher one - else
        # the newer search is aborted and its pushes dropped, stranding the UI.
        api = self._api()
        api.start_search('q', [], {}, 8)
        api.start_search('q', [], {}, 7)
        self.assertEqual(api._search_seq, 8)

    def test_bootstrap_resets_seq_so_a_reloaded_page_is_not_stranded(self) -> None:
        # A page reload restarts the UI's seq counter at 0; the backend must reset
        # too, or the monotonic guard would reject every new (lower) seq forever.
        api = self._api()
        api.start_search('q', [], {}, 42)
        self.assertEqual(api._search_seq, 42)

        api.get_bootstrap()
        self.assertEqual(api._search_seq, 0)

        api.start_search('q', [], {}, 1)
        self.assertEqual(api._search_seq, 1)


if __name__ == '__main__':
    unittest.main()
