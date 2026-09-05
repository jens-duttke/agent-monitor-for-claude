"""
Tests for the Linux process backend.

Everything the backend decides is derived from one ``/proc`` scan, so the cases
here feed it a fabricated table rather than the machine's real one - the
liveness gate, the recycled-pid guard, the host classification and its handling
of the kernel's 15-character name truncation.  A few cases do probe this very
process, which is the one pid guaranteed to be alive while the suite runs.

Skipped as a whole on Windows.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux process backend')

if sys.platform != 'win32':
    from agent_monitor_for_claude import procfs
    from agent_monitor_for_claude.platforms import process_linux
    from agent_monitor_for_claude.process_probe import TERMINAL_WINDOW_OWNERS, probe


def _entry(comm: str, ppid: int, starttime: int, rss_pages: int | None = 100, cpu_ticks: int | None = 0):
    """Build one parsed procfs entry."""
    return procfs.ProcEntry(comm=comm, ppid=ppid, starttime=starttime, rss_pages=rss_pages, cpu_ticks=cpu_ticks)


@_LINUX_ONLY
class ClassifyAncestryTest(unittest.TestCase):
    """The first GUI host on the chain names the host; a shell before it means the CLI drove it."""

    def test_editor_without_a_shell(self) -> None:
        self.assertEqual(process_linux._classify_ancestry(['code', 'code', 'systemd']), ('VS Code', False))

    def test_shell_before_the_editor_means_via_cli(self) -> None:
        self.assertEqual(process_linux._classify_ancestry(['bash', 'code', 'systemd']), ('VS Code', True))

    def test_terminal_emulator_is_a_gui_host(self) -> None:
        self.assertEqual(process_linux._classify_ancestry(['bash', 'konsole', 'systemd']), ('Konsole', True))

    def test_shell_alone_is_the_host(self) -> None:
        self.assertEqual(process_linux._classify_ancestry(['zsh', 'systemd']), ('Zsh', True))

    def test_nothing_recognised(self) -> None:
        self.assertEqual(process_linux._classify_ancestry(['systemd']), (None, False))
        self.assertEqual(process_linux._classify_ancestry([]), (None, False))

    def test_a_truncated_name_is_still_recognised(self) -> None:
        # The kernel caps comm at 15 characters, so gnome-terminal-server only
        # ever arrives as 'gnome-terminal-'; without the truncated alias every
        # GNOME Terminal session would report no host at all.
        self.assertEqual(len('gnome-terminal-'), 15)
        self.assertEqual(process_linux._classify_ancestry(['bash', 'gnome-terminal-']), ('GNOME Terminal', True))


@_LINUX_ONLY
class TerminalWindowOwnersTest(unittest.TestCase):
    """The title fallback only raises windows owned by a terminal emulator."""

    def test_terminal_emulators_are_owners(self) -> None:
        for owner in ('konsole', 'alacritty', 'gnome-terminal-', 'xterm'):
            self.assertIn(owner, TERMINAL_WINDOW_OWNERS)

    def test_editors_are_not_terminal_owners(self) -> None:
        for editor in ('code', 'cursor', 'pycharm'):
            self.assertNotIn(editor, TERMINAL_WINDOW_OWNERS)


@_LINUX_ONLY
class ProbeAllTest(unittest.TestCase):
    """Liveness, the recycled-pid guard and the child count, over a fabricated table."""

    _SESSION = 1000

    def _table(self) -> dict[int, object]:
        return {
            1: _entry('systemd', 0, 10),
            900: _entry('code', 1, 500),
            self._SESSION: _entry('claude', 900, 1000),
            1001: _entry('node', self._SESSION, 1005),          # started with the session: a helper
            1002: _entry('rg', self._SESSION, 400_000),         # started much later: a running tool
        }

    def _probe(self, requests):
        table = self._table()
        with mock.patch.object(procfs, 'read_proc_table', return_value=table), \
             mock.patch.object(procfs, 'clk_tck', return_value=100):
            return process_linux.probe_all(requests)

    def test_alive_with_the_recorded_start_time(self) -> None:
        info = self._probe([(self._SESSION, 1000)])[self._SESSION]
        self.assertTrue(info.alive)
        self.assertEqual(info.host, 'VS Code')
        self.assertFalse(info.via_cli)

    def test_a_helper_started_with_the_session_is_not_a_running_tool(self) -> None:
        # A stdio MCP server or watcher starts alongside the session; counting it
        # would make every session read as busy for its whole lifetime.
        info = self._probe([(self._SESSION, 1000)])[self._SESSION]
        self.assertEqual(info.child_count, 1)
        self.assertTrue(info.tool_running)

    def test_a_mismatched_start_time_reads_as_not_alive(self) -> None:
        # The kernel recycled the pid onto an unrelated process, so the registry
        # record is stale.
        info = self._probe([(self._SESSION, 999)])[self._SESSION]
        self.assertFalse(info.alive)
        self.assertEqual(info.child_count, 0)

    def test_start_time_zero_is_compared_like_any_other(self) -> None:
        # Ticks are counted since boot, so 0 is a real start time, never a
        # sentinel for "unknown" - skipping the comparison for it would disable
        # the recycled-pid guard for the very first processes on the machine.
        info = self._probe([(self._SESSION, 0)])[self._SESSION]
        self.assertFalse(info.alive)

    def test_an_absent_pid_reads_as_not_alive(self) -> None:
        self.assertFalse(self._probe([(4242, None)])[4242].alive)

    def test_an_unreadable_proc_degrades_rather_than_raising(self) -> None:
        with mock.patch.object(procfs, 'read_proc_table', return_value={}):
            self.assertFalse(process_linux.probe_all([(self._SESSION, None)])[self._SESSION].alive)


@_LINUX_ONLY
class LiveProcessTest(unittest.TestCase):
    """A few readings against the running interpreter, the one pid known to exist."""

    def test_this_process_reads_as_alive(self) -> None:
        self.assertTrue(probe(os.getpid()).alive)

    def test_ancestry_reaches_at_least_the_parent(self) -> None:
        chain = process_linux.ancestry(os.getpid())
        self.assertTrue(chain)
        self.assertEqual(chain[0][0], os.getppid())

    def test_process_names_are_lowercased(self) -> None:
        names = process_linux.process_names()
        self.assertIn(os.getpid(), names)
        self.assertEqual(names[os.getpid()], names[os.getpid()].lower())

    def test_a_stale_start_time_yields_no_stats(self) -> None:
        self.assertEqual(process_linux.process_stats(os.getpid(), 1), [])

    def test_no_wsl_vm_row_exists_here(self) -> None:
        # vmmem is a Windows-host process; WSL discovery is gated on it, so a
        # Linux host must report it absent rather than go looking for distros.
        self.assertFalse(process_linux.vmmem_present())


if __name__ == '__main__':
    unittest.main()
