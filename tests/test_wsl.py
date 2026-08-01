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


if __name__ == '__main__':
    unittest.main()
