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


def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int) -> None:
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields3plus = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
                   '50', '10', '0', '0', '20', '0', '4', '0', str(starttime), '1000000', '500']
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields3plus), encoding='utf-8')


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
    def _root(self, base: str):
        return wsl.SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                               proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')

    def test_liveness_and_recycled_pid(self):
        with tempfile.TemporaryDirectory() as base:
            root = self._root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            self.assertTrue(wsl.probe_wsl_sessions(root, [(100, 5000)])[100].alive)
            self.assertFalse(wsl.probe_wsl_sessions(root, [(100, 4999)])[100].alive)   # recycled
            self.assertFalse(wsl.probe_wsl_sessions(root, [(200, None)])[200].alive)   # gone

    def test_proc_start_ticks_zero_is_not_treated_as_absent(self):
        # starttime 0 is a legal stat-field value (ticks since boot), not a sentinel for "unknown" -
        # a falsy `if proc_start_ticks:` check would silently skip the recycled-pid comparison here.
        with tempfile.TemporaryDirectory() as base:
            root = self._root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 0)
            self.assertTrue(wsl.probe_wsl_sessions(root, [(100, 0)])[100].alive)        # matches exactly
            _write_stat(root.proc_dir, 200, 'claude', 1, 7000)
            self.assertFalse(wsl.probe_wsl_sessions(root, [(200, 0)])[200].alive)       # recycled: 0 != 7000

    def test_descendants_and_helper_window(self):
        with tempfile.TemporaryDirectory() as base:
            root = self._root(base)
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
        root = self._root(tempfile.mkdtemp())
        root = wsl.SessionRoot(origin='wsl:U', label='U', config_dir=root.config_dir,
                               proc_dir=root.proc_dir / 'missing', temp_dir=root.temp_dir)
        self.assertFalse(wsl.probe_wsl_sessions(root, [(1, None)])[1].alive)


if __name__ == '__main__':
    unittest.main()
