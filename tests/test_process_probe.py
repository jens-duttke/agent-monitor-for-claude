"""Tests for the process probe's PID-recycling detection."""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta

import psutil

from agent_monitor_for_claude.process_probe import (
    TERMINAL_WINDOW_OWNERS,
    probe,
    _classify_ancestry,
    _is_child_link_real,
    _meaningful_children,
    _ticks_match_epoch,
)


def _to_ticks(moment: datetime) -> int:
    """Convert a naive local datetime to .NET ticks (100 ns since year 1)."""
    return int((moment - datetime(1, 1, 1)).total_seconds() * 10_000_000)


class TicksMatchTest(unittest.TestCase):
    def test_matching_times(self) -> None:
        epoch = time.time()
        ticks = _to_ticks(datetime.fromtimestamp(epoch))
        self.assertTrue(_ticks_match_epoch(ticks, epoch))

    def test_small_clock_skew_tolerated(self) -> None:
        epoch = time.time()
        ticks = _to_ticks(datetime.fromtimestamp(epoch) + timedelta(seconds=5))
        self.assertTrue(_ticks_match_epoch(ticks, epoch))

    def test_recycled_pid_mismatch(self) -> None:
        epoch = time.time()
        ticks = _to_ticks(datetime.fromtimestamp(epoch) - timedelta(hours=1))
        self.assertFalse(_ticks_match_epoch(ticks, epoch))

    def test_oversized_ticks_degrade_to_mismatch(self) -> None:
        # A corrupted registry procStart can exceed the representable date range;
        # the comparison must degrade to a mismatch, never raise OverflowError
        # (which would crash the whole snapshot).
        self.assertFalse(_ticks_match_epoch(10 ** 20, time.time()))


class ClassifyAncestryTest(unittest.TestCase):
    def test_extension_session_is_plain_vscode(self) -> None:
        self.assertEqual(_classify_ancestry(['code.exe', 'code.exe', 'explorer.exe']), ('VS Code', False))

    def test_cli_in_integrated_terminal(self) -> None:
        self.assertEqual(_classify_ancestry(['cmd.exe', 'code.exe', 'explorer.exe']), ('VS Code', True))

    def test_cli_in_windows_terminal(self) -> None:
        self.assertEqual(_classify_ancestry(['pwsh.exe', 'windowsterminal.exe', 'explorer.exe']), ('Windows Terminal', True))

    def test_bare_shell_without_gui_host(self) -> None:
        self.assertEqual(_classify_ancestry(['pwsh.exe', 'explorer.exe']), ('PowerShell', True))

    def test_unknown_chain(self) -> None:
        self.assertEqual(_classify_ancestry(['explorer.exe']), (None, False))

    def test_empty_chain(self) -> None:
        self.assertEqual(_classify_ancestry([]), (None, False))


class TerminalWindowOwnersTest(unittest.TestCase):
    """The title fallback only raises windows owned by a terminal or console host."""

    def test_terminal_emulators_and_console_hosts_are_owners(self) -> None:
        for owner in ('windowsterminal.exe', 'wezterm-gui.exe', 'conhost.exe', 'openconsole.exe'):
            self.assertIn(owner, TERMINAL_WINDOW_OWNERS)

    def test_editors_are_not_terminal_owners(self) -> None:
        for editor in ('code.exe', 'devenv.exe', 'pycharm64.exe'):
            self.assertNotIn(editor, TERMINAL_WINDOW_OWNERS)


class MeaningfulChildrenTest(unittest.TestCase):
    """The descendant walk must reject stale parent links from PID reuse."""

    _SESSION = 1000

    def _children(self, table: dict, cache: dict) -> list[str]:
        children_index: dict[int, list[int]] = {}
        for pid, (ppid, _name) in table.items():
            children_index.setdefault(ppid, []).append(pid)
        return _meaningful_children(self._SESSION, table, children_index, cache)

    def test_real_subtree_counted_and_console_excluded(self) -> None:
        table = {
            1000: (1, 'claude.exe'),
            1001: (1000, 'node.exe'),
            1002: (1001, 'esbuild.exe'),
            1003: (1000, 'conhost.exe'),
        }
        cache = {1000: 5000.0, 1001: 5001.0, 1002: 5002.0, 1003: 5000.5}
        self.assertEqual(sorted(set(self._children(table, cache))), ['esbuild.exe', 'node.exe'])

    def test_orphan_with_recycled_parent_pid_is_pruned(self) -> None:
        # 2000 claims the session PID as parent but started at boot, long before
        # the session - a stale link. Its own subtree (2001) must not leak in.
        table = {
            1000: (1, 'claude.exe'),
            1001: (1000, 'node.exe'),
            2000: (1000, 'lsass.exe'),
            2001: (2000, 'services.exe'),
        }
        cache = {1000: 5000.0, 1001: 5001.0, 2000: 100.0, 2001: 90.0}
        self.assertEqual(sorted(set(self._children(table, cache))), ['node.exe'])

    def test_unverifiable_link_rejected(self) -> None:
        table = {1000: (1, 'claude.exe'), 3000: (1000, 'protected.exe')}
        cache = {1000: 5000.0, 3000: None}
        self.assertEqual(self._children(table, cache), [])


class ChildLinkTest(unittest.TestCase):
    def test_child_started_after_parent_is_real(self) -> None:
        self.assertTrue(_is_child_link_real(1, 2, {1: 10.0, 2: 11.0}))

    def test_child_older_than_parent_is_rejected(self) -> None:
        self.assertFalse(_is_child_link_real(1, 2, {1: 10.0, 2: 5.0}))

    def test_unknown_start_time_is_rejected(self) -> None:
        self.assertFalse(_is_child_link_real(1, 2, {1: 10.0, 2: None}))
        self.assertFalse(_is_child_link_real(1, 2, {1: None, 2: 11.0}))


class ProbeRecyclingTest(unittest.TestCase):
    def test_probe_accepts_matching_start_time(self) -> None:
        pid = os.getpid()
        create_time = psutil.Process(pid).create_time()
        ticks = _to_ticks(datetime.fromtimestamp(create_time))

        self.assertTrue(probe(pid, ticks).alive)

    def test_probe_detects_recycled_pid(self) -> None:
        pid = os.getpid()
        create_time = psutil.Process(pid).create_time()
        stale_ticks = _to_ticks(datetime.fromtimestamp(create_time) - timedelta(hours=2))

        self.assertFalse(probe(pid, stale_ticks).alive)

    def test_probe_without_ticks_keeps_previous_behavior(self) -> None:
        self.assertTrue(probe(os.getpid()).alive)


if __name__ == '__main__':
    unittest.main()
