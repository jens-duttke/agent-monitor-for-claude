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


if __name__ == '__main__':
    unittest.main()
