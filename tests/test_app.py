"""Tests for the _MonitorApi bridge behavior."""
from __future__ import annotations

import io
import unittest
from unittest import mock

from agent_monitor_for_claude import app
from agent_monitor_for_claude.app import _LOG_MAX_LEN, _MonitorApi, _sanitize_log


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
