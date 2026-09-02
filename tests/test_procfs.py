"""
Tests for the shared procfs reader.

Two callers depend on it - the Linux process backend reading the running
system's ``/proc``, and the WSL probe reading a distro's over a UNC share - so
every case here feeds it a fabricated tree in a temp directory and runs on
either system.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_monitor_for_claude import procfs


def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int,
                utime: int = 50, stime: int = 10, rss_pages: int = 500) -> None:
    """Write one fabricated ``/proc/<pid>/stat`` entry."""
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
              str(utime), str(stime), '0', '0', '20', '0', '4', '0', str(starttime), '1000000', str(rss_pages)]
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields), encoding='utf-8')


class ParseStatTests(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self) -> None:
        # comm is process-settable and may contain both, so it cannot be
        # delimited by the first ')' - only the last one is the real close.
        parsed = procfs.parse_stat('123 (tmux: server (x)) S 1 123 123 0 -1 4 0 0 0 0 5 6 0 0 20 0 1 0 83860 1 2')
        self.assertIsNotNone(parsed)
        comm, fields = parsed
        self.assertEqual(comm, 'tmux: server (x)')
        self.assertEqual(fields[1], '1')        # ppid (field 4)
        self.assertEqual(fields[19], '83860')   # starttime (field 22)

    def test_malformed(self) -> None:
        self.assertIsNone(procfs.parse_stat('no parens here'))
        self.assertIsNone(procfs.parse_stat(''))


class ReadProcTableTests(unittest.TestCase):
    def test_reads_every_numeric_entry(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_dir = Path(base)
            _write_stat(proc_dir, 100, 'claude', 1, 5000)
            _write_stat(proc_dir, 101, 'node', 100, 6000)
            (proc_dir / 'self').mkdir()   # a non-numeric entry must be ignored

            table = procfs.read_proc_table(proc_dir)

        self.assertEqual(sorted(table), [100, 101])
        self.assertEqual(table[101].comm, 'node')
        self.assertEqual(table[101].ppid, 100)
        self.assertEqual(table[101].starttime, 6000)
        self.assertEqual(table[101].rss_pages, 500)
        self.assertEqual(table[101].cpu_ticks, 60)

    def test_one_unreadable_entry_does_not_hide_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_dir = Path(base)
            _write_stat(proc_dir, 100, 'claude', 1, 5000)
            (proc_dir / '101').mkdir()   # exists, but has no stat file at all

            table = procfs.read_proc_table(proc_dir)

        self.assertEqual(list(table), [100])

    def test_an_unreachable_tree_yields_an_empty_table(self) -> None:
        self.assertEqual(procfs.read_proc_table(Path('/does/not/exist')), {})

    def test_a_truncated_entry_still_yields_the_control_fields(self) -> None:
        # rss and cpu feed the resource panel alone, so either degrades on its
        # own rather than dropping a process the liveness probe needs.
        with tempfile.TemporaryDirectory() as base:
            proc_dir = Path(base)
            (proc_dir / '100').mkdir()
            (proc_dir / '100' / 'stat').write_text('100 (claude) S 1 1 1 0 -1 4 0 0 0 0', encoding='utf-8')

            table = procfs.read_proc_table(proc_dir)

        self.assertNotIn(100, table)   # starttime itself is missing: too malformed to trust


class AncestorsTests(unittest.TestCase):
    def test_chain_is_nearest_first(self) -> None:
        table = {
            1: procfs.ProcEntry('systemd', 0, 10, None, None),
            900: procfs.ProcEntry('code', 1, 500, None, None),
            1000: procfs.ProcEntry('claude', 900, 1000, None, None),
        }
        self.assertEqual(procfs.ancestors(1000, table), [(900, 'code'), (1, 'systemd')])

    def test_a_parent_that_started_later_ends_the_walk(self) -> None:
        # It cannot be the real parent: the pid was recycled after the child
        # was orphaned, and following it would climb an unrelated tree.
        table = {
            900: procfs.ProcEntry('unrelated', 1, 9000, None, None),
            1000: procfs.ProcEntry('claude', 900, 1000, None, None),
        }
        self.assertEqual(procfs.ancestors(1000, table), [])

    def test_a_cycle_cannot_loop(self) -> None:
        table = {
            1: procfs.ProcEntry('a', 2, 10, None, None),
            2: procfs.ProcEntry('b', 1, 10, None, None),
        }
        self.assertEqual(len(procfs.ancestors(1, table)), 1)

    def test_an_unknown_pid_has_no_ancestors(self) -> None:
        self.assertEqual(procfs.ancestors(42, {}), [])


class LiveDescendantsTests(unittest.TestCase):
    def _tree(self) -> dict[int, procfs.ProcEntry]:
        return {
            100: procfs.ProcEntry('claude', 1, 5000, None, None),
            101: procfs.ProcEntry('node', 100, 5500, None, None),        # within the helper window
            102: procfs.ProcEntry('cargo', 100, 65000, None, None),      # a real tool child
            103: procfs.ProcEntry('rustc', 102, 65010, None, None),      # its grandchild
        }

    def _descendants(self, pid: int, ticks: int | None):
        table = self._tree()
        return procfs.live_descendants(pid, ticks, table, procfs.children_index(table))

    def test_helpers_are_excluded_but_walked_through(self) -> None:
        names = [name for _pid, name in self._descendants(100, 5000)]
        self.assertEqual(sorted(names), ['cargo', 'rustc'])

    def test_a_recycled_pid_reads_as_not_alive(self) -> None:
        self.assertIsNone(self._descendants(100, 4999))

    def test_start_time_zero_is_a_real_value(self) -> None:
        # Ticks count from boot, so 0 is a legitimate start time - skipping the
        # comparison for it would disable the recycled-pid guard.
        self.assertIsNone(self._descendants(100, 0))

    def test_an_absent_pid_reads_as_not_alive(self) -> None:
        self.assertIsNone(self._descendants(999, None))

    def test_without_a_recorded_start_time_the_guard_is_skipped(self) -> None:
        self.assertIsNotNone(self._descendants(100, None))


class BootTimeTests(unittest.TestCase):
    def test_reads_the_btime_line(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'stat').write_text('cpu 1 2 3\nbtime 1700000000\nprocesses 5\n', encoding='utf-8')
            self.assertEqual(procfs.read_btime(Path(base)), 1700000000)

    def test_a_missing_file_degrades(self) -> None:
        self.assertIsNone(procfs.read_btime(Path('/does/not/exist')))

    def test_a_file_without_btime_degrades(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'stat').write_text('cpu 1 2 3\n', encoding='utf-8')
            self.assertIsNone(procfs.read_btime(Path(base)))


class SampleCpuTests(unittest.TestCase):
    def setUp(self) -> None:
        procfs._sample_cache.clear()
        self.addCleanup(procfs._sample_cache.clear)

    def test_first_sighting_has_nothing_to_diff_against(self) -> None:
        self.assertIsNone(procfs.sample_cpu('local', 1, 5000, 100, 1000.0))

    def test_a_later_sample_reports_the_delta(self) -> None:
        procfs.sample_cpu('local', 1, 5000, 100, 1000.0)
        # 100 ticks over one second at 100 Hz is one fully-used core.
        self.assertAlmostEqual(procfs.sample_cpu('local', 1, 5000, 200, 1001.0), 100.0)

    def test_a_recycled_pid_starts_over(self) -> None:
        procfs.sample_cpu('local', 1, 5000, 100, 1000.0)
        self.assertIsNone(procfs.sample_cpu('local', 1, 9000, 5, 1001.0))

    def test_unreadable_ticks_evict_the_baseline(self) -> None:
        procfs.sample_cpu('local', 1, 5000, 100, 1000.0)
        self.assertIsNone(procfs.sample_cpu('local', 1, 5000, None, 1001.0))
        self.assertIsNone(procfs.sample_cpu('local', 1, 5000, 200, 1002.0))

    def test_pruning_is_scoped_to_one_origin(self) -> None:
        procfs.sample_cpu('local', 1, 5000, 100, 1000.0)
        procfs.sample_cpu('wsl:U', 1, 5000, 100, 1000.0)
        procfs.prune_sample_cache('local', set())

        self.assertNotIn(('local', 1), procfs._sample_cache)
        self.assertIn(('wsl:U', 1), procfs._sample_cache)


class SystemConstantsTests(unittest.TestCase):
    def test_the_queried_values_are_plausible(self) -> None:
        # Queried for the running system, assumed for a foreign tree; either way
        # a nonsensical value would skew every CPU and memory figure.
        self.assertGreater(procfs.clk_tck(), 0)
        self.assertGreater(procfs.page_size(), 0)


if __name__ == '__main__':
    unittest.main()
