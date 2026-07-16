"""Tests for the _MonitorApi bridge behavior."""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude.app import _MonitorApi


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
