"""Tests for WSL distro discovery - all without WSL installed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import wsl


class ParseDistroListTests(unittest.TestCase):
    def test_utf16_output(self):
        raw = 'Ubuntu\r\ndocker-desktop\r\n'.encode('utf-16-le')
        self.assertEqual(wsl._parse_distro_list(raw), ['Ubuntu', 'docker-desktop'])

    def test_empty_and_garbage(self):
        self.assertEqual(wsl._parse_distro_list(b''), [])
        self.assertEqual(wsl._parse_distro_list('\r\n\r\n'.encode('utf-16-le')), [])


class DiscoverRootsTests(unittest.TestCase):
    def test_home_and_root_claude(self):
        with tempfile.TemporaryDirectory() as base:
            claude = Path(base) / 'Ubuntu' / 'home' / 'dev' / '.claude'
            claude.mkdir(parents=True)
            (Path(base) / 'Ubuntu' / 'root').mkdir(parents=True)
            (Path(base) / 'docker-desktop').mkdir()  # no .claude anywhere
            roots = wsl._discover_roots(['Ubuntu', 'docker-desktop'], Path(base))
            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].origin, 'wsl:Ubuntu')
            self.assertEqual(roots[0].label, 'Ubuntu')
            self.assertEqual(roots[0].config_dir, claude)
            self.assertEqual(roots[0].proc_dir, Path(base) / 'Ubuntu' / 'proc')
            self.assertEqual(roots[0].temp_dir, Path(base) / 'Ubuntu' / 'tmp')

    def test_stopped_distro_never_globbed(self):
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'Stopped' / 'home' / 'dev' / '.claude').mkdir(parents=True)
            self.assertEqual(wsl._discover_roots([], Path(base)), [])

    def test_real_unc_base_joins_to_a_double_backslash_root(self):
        # Regression guard: Path(r'\\wsl.localhost') alone collapses to a
        # single leading backslash the instant it is constructed (pathlib
        # only recognizes a UNC root when server and share appear together
        # in one parse), and no Path built from it can regain the doubled
        # separator afterwards - so _UNC_BASE must stay a plain string and
        # _distro_roots must join server and share into one string before
        # Path() ever sees either. Verified here by capturing the exact
        # paths is_dir() is asked about against the real _UNC_BASE, without
        # ever touching the network (Path.is_dir is stubbed out).
        checked: list[str] = []

        def fake_is_dir(path):
            checked.append(str(path))
            return False

        with mock.patch('pathlib.Path.is_dir', fake_is_dir):
            self.assertEqual(wsl._discover_roots(['Ubuntu'], wsl._UNC_BASE), [])

        self.assertTrue(checked)
        for path in checked:
            self.assertTrue(path.startswith('\\\\wsl.localhost\\Ubuntu'), path)

    def test_unreadable_candidate_is_skipped_not_the_whole_distro(self):
        # Real-machine bug: root/.claude is routinely unreadable (WinError 5,
        # PermissionError) over the 9P share - Path.is_dir() only swallows
        # not-found-style errors, never a permission error, so it used to
        # propagate out of _distro_roots and get caught by _discover_roots's
        # per-distro `except OSError`, dropping the ENTIRE distro - including
        # a perfectly good home candidate found moments earlier. Only the
        # poisoned candidate must be skipped; its sibling must still be
        # found. Exercised against the real is_dir() codepath (patched to
        # raise for exactly one path) rather than mocking the helper, so this
        # pins the actual failure mode, not just the helper's own contract.
        with tempfile.TemporaryDirectory() as base:
            claude = Path(base) / 'Ubuntu' / 'home' / 'dev' / '.claude'
            claude.mkdir(parents=True)
            poisoned = Path(base) / 'Ubuntu' / 'root' / '.claude'
            real_is_dir = Path.is_dir

            def flaky_is_dir(path):
                if path == poisoned:
                    raise PermissionError(5, 'Access is denied')
                return real_is_dir(path)

            with mock.patch.object(Path, 'is_dir', flaky_is_dir):
                roots = wsl._discover_roots(['Ubuntu'], Path(base))

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].origin, 'wsl:Ubuntu')
            self.assertEqual(roots[0].config_dir, claude)

    def test_unreadable_home_subdir_is_skipped_root_claude_still_found(self):
        # Mirror case: a permission error on one *user's* .claude (someone
        # else's home directory, unreadable to this account) must not hide
        # root/.claude, which this distro does expose.
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'Ubuntu' / 'home' / 'otheruser').mkdir(parents=True)
            poisoned = Path(base) / 'Ubuntu' / 'home' / 'otheruser' / '.claude'
            root_claude = Path(base) / 'Ubuntu' / 'root' / '.claude'
            root_claude.mkdir(parents=True)
            real_is_dir = Path.is_dir

            def flaky_is_dir(path):
                if path == poisoned:
                    raise PermissionError(5, 'Access is denied')
                return real_is_dir(path)

            with mock.patch.object(Path, 'is_dir', flaky_is_dir):
                roots = wsl._discover_roots(['Ubuntu'], Path(base))

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].origin, 'wsl:Ubuntu')
            self.assertEqual(roots[0].config_dir, root_claude)


class IsReadableDirTests(unittest.TestCase):
    """Pins _is_readable_dir's own contract directly, isolated from _discover_roots."""

    def test_real_directory_is_true(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertTrue(wsl._is_readable_dir(Path(base)))

    def test_missing_path_is_false(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertFalse(wsl._is_readable_dir(Path(base) / 'does-not-exist'))

    def test_permission_error_is_false(self):
        with mock.patch.object(Path, 'is_dir', side_effect=PermissionError(5, 'Access is denied')):
            self.assertFalse(wsl._is_readable_dir(Path('irrelevant')))


class WslRootsGateTests(unittest.TestCase):
    def setUp(self):
        wsl.reset_caches()
        self.addCleanup(wsl.reset_caches)

    def test_no_vmmem_short_circuits(self):
        with mock.patch.object(wsl, '_vmmem_present', return_value=False), \
             mock.patch.object(wsl, '_list_running_distros') as listing:
            self.assertEqual(wsl.wsl_roots(), [])
            listing.assert_not_called()

    def test_setting_off_short_circuits(self):
        with mock.patch.object(wsl, 'WSL_MONITORING', False), \
             mock.patch.object(wsl, '_vmmem_present') as probe:
            self.assertEqual(wsl.wsl_roots(), [])
            probe.assert_not_called()

    def test_discovery_cached_within_ttl(self):
        with mock.patch.object(wsl, '_vmmem_present', return_value=True), \
             mock.patch.object(wsl, '_list_running_distros', return_value=[]) as listing:
            wsl.wsl_roots()
            wsl.wsl_roots()
            self.assertEqual(listing.call_count, 1)


def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int,
                 utime: int = 50, stime: int = 10, rss_pages: int = 500) -> None:
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields3plus = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
                   str(utime), str(stime), '0', '0', '20', '0', '4', '0', str(starttime), '1000000', str(rss_pages)]
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields3plus), encoding='utf-8')


def _write_proc_stat(proc_dir: Path, btime: int) -> None:
    """Write a fake ``/proc/stat`` at the proc_dir root, carrying a ``btime`` line among others."""
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / 'stat').write_text(f'cpu  100 0 200 300 0 0 0 0 0 0\nbtime {btime}\nprocesses 500\n', encoding='utf-8')


def _wsl_root(base: str) -> wsl.SessionRoot:
    return wsl.SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                           proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')


class ParseStatTests(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self):
        parsed = wsl._parse_stat('123 (tmux: server (x)) S 1 123 123 0 -1 4 0 0 0 0 5 6 0 0 20 0 1 0 83860 1 2')
        self.assertIsNotNone(parsed)
        comm, fields = parsed
        self.assertEqual(comm, 'tmux: server (x)')
        self.assertEqual(fields[1], '1')        # ppid (field 4)
        self.assertEqual(fields[19], '83860')   # starttime (field 22)

    def test_malformed(self):
        self.assertIsNone(wsl._parse_stat('no parens here'))


class ProbeWslSessionsTests(unittest.TestCase):
    def test_liveness_and_recycled_pid(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            self.assertTrue(wsl.probe_wsl_sessions(root, [(100, 5000)])[100].alive)
            self.assertFalse(wsl.probe_wsl_sessions(root, [(100, 4999)])[100].alive)   # recycled
            self.assertFalse(wsl.probe_wsl_sessions(root, [(200, None)])[200].alive)   # gone

    def test_proc_start_ticks_zero_is_not_treated_as_absent(self):
        # starttime 0 is a legal stat-field value (ticks since boot), not a sentinel for "unknown" -
        # a falsy `if proc_start_ticks:` check would silently skip the recycled-pid comparison here.
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 0)
            self.assertTrue(wsl.probe_wsl_sessions(root, [(100, 0)])[100].alive)        # matches exactly
            _write_stat(root.proc_dir, 200, 'claude', 1, 7000)
            self.assertFalse(wsl.probe_wsl_sessions(root, [(200, 0)])[200].alive)       # recycled: 0 != 7000

    def test_descendants_and_helper_window(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 500)     # helper: within 10 s * 100 ticks
            _write_stat(root.proc_dir, 102, 'cargo', 100, 5000 + 60000)  # real tool child
            _write_stat(root.proc_dir, 103, 'rustc', 102, 5000 + 60010)  # grandchild
            info = wsl.probe_wsl_sessions(root, [(100, 5000)])[100]
            self.assertTrue(info.alive)
            self.assertEqual(info.child_count, 2)
            self.assertTrue(info.tool_running)
            self.assertIsNone(info.host)

    def test_unreadable_proc_dir(self):
        root = _wsl_root(tempfile.mkdtemp())
        root = wsl.SessionRoot(origin='wsl:U', label='U', config_dir=root.config_dir,
                               proc_dir=root.proc_dir / 'missing', temp_dir=root.temp_dir)
        self.assertFalse(wsl.probe_wsl_sessions(root, [(1, None)])[1].alive)


class WslProcessStatsTests(unittest.TestCase):
    """Covers wsl_process_stats: the same descendant/liveness rules as probe_wsl_sessions, plus
    memory/uptime read straight from procfs and CPU sampled against a prior call."""

    def setUp(self):
        wsl._wsl_sample_cache.clear()
        self.addCleanup(wsl._wsl_sample_cache.clear)

    def test_first_call_yields_no_cpu_with_correct_rss_and_uptime(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            btime = 1700000000
            _write_proc_stat(root.proc_dir, btime)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)               # session process
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 60000)       # real tool child

            now = 1700100000.0
            with mock.patch.object(wsl.time, 'time', return_value=now):
                stats = wsl.wsl_process_stats(root, 100, 5000)

        self.assertEqual(len(stats), 1)
        stat = stats[0]
        self.assertEqual(stat.pid, 101)
        self.assertEqual(stat.name, 'node')
        self.assertIsNone(stat.cpu_percent)
        self.assertEqual(stat.rss_bytes, 500 * 4096)
        expected_uptime = now - (btime + (5000 + 60000) / wsl._CLK_TCK)
        self.assertAlmostEqual(stat.uptime_seconds, expected_uptime, places=6)
        self.assertEqual(stat.kind, 'process')

    def test_second_call_reports_cpu_delta(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_proc_stat(root.proc_dir, 1700000000)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 60000, utime=50, stime=10)

            now = 1700100000.0
            with mock.patch.object(wsl.time, 'time', return_value=now):
                first = wsl.wsl_process_stats(root, 100, 5000)
            self.assertIsNone(first[0].cpu_percent)

            # Same starttime (not recycled), utime/stime bumped by 40+20 ticks, clock advanced 1 s.
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 60000, utime=90, stime=30)
            with mock.patch.object(wsl.time, 'time', return_value=now + 1.0):
                second = wsl.wsl_process_stats(root, 100, 5000)

        self.assertEqual(len(second), 1)
        expected_cpu = (60 / wsl._CLK_TCK) / 1.0 * 100.0
        self.assertAlmostEqual(second[0].cpu_percent, expected_cpu, places=6)

    def test_recycled_child_starttime_resets_cpu_to_none(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_proc_stat(root.proc_dir, 1700000000)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 60000, utime=50, stime=10)

            now = 1700100000.0
            with mock.patch.object(wsl.time, 'time', return_value=now):
                wsl.wsl_process_stats(root, 100, 5000)   # primes the baseline

            with mock.patch.object(wsl.time, 'time', return_value=now + 1.0):
                established = wsl.wsl_process_stats(root, 100, 5000)
            self.assertIsInstance(established[0].cpu_percent, float)   # a real reading exists now

            # Pid 101 recycled: same numeric pid, a new process with a later starttime.
            _write_stat(root.proc_dir, 101, 'python', 100, 5000 + 61000, utime=1, stime=1)
            with mock.patch.object(wsl.time, 'time', return_value=now + 2.0):
                stats = wsl.wsl_process_stats(root, 100, 5000)

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0].name, 'python')
        self.assertIsNone(stats[0].cpu_percent)

    def test_dead_or_stale_session_pid_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)
            _write_proc_stat(root.proc_dir, 1700000000)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)

            with mock.patch.object(wsl.time, 'time', return_value=1700100000.0):
                self.assertEqual(wsl.wsl_process_stats(root, 999, None), [])    # absent pid
                self.assertEqual(wsl.wsl_process_stats(root, 100, 4999), [])    # starttime mismatch

    def test_prune_is_scoped_to_this_origin(self):
        with tempfile.TemporaryDirectory() as base:
            root = _wsl_root(base)   # origin 'wsl:U'
            _write_proc_stat(root.proc_dir, 1700000000)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 60000)

            other_key = ('wsl:Other', 555)
            wsl._wsl_sample_cache[other_key] = (123, 60, 1700000000.0)

            with mock.patch.object(wsl.time, 'time', return_value=1700100000.0):
                wsl.wsl_process_stats(root, 100, 5000)

        self.assertIn(other_key, wsl._wsl_sample_cache)


if __name__ == '__main__':
    unittest.main()
