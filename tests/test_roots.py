"""Tests for session-root enumeration and origin lookup."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import roots
from agent_monitor_for_claude.paths import SessionRoot, windows_root


def _fake_wsl_root() -> SessionRoot:
    return SessionRoot(origin='wsl:U', label='U', config_dir=Path('cfg'), proc_dir=Path('proc'), temp_dir=Path('tmp'))


class SessionRootsTests(unittest.TestCase):
    def test_windows_root_then_wsl_roots(self) -> None:
        fake_root = _fake_wsl_root()
        with mock.patch.object(roots, 'wsl_roots', return_value=[fake_root]):
            result = roots.session_roots()

        # windows_root() builds a fresh instance on every call, so compare fields, not identity.
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].origin, windows_root().origin)
        self.assertEqual(result[1], fake_root)

    def test_no_wsl_roots_returns_windows_only(self) -> None:
        with mock.patch.object(roots, 'wsl_roots', return_value=[]):
            result = roots.session_roots()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].origin, 'windows')


class RootForOriginTests(unittest.TestCase):
    def test_finds_wsl_root_by_exact_origin(self) -> None:
        fake_root = _fake_wsl_root()
        with mock.patch.object(roots, 'wsl_roots', return_value=[fake_root]):
            found = roots.root_for_origin('wsl:U')

        self.assertEqual(found, fake_root)

    def test_finds_windows_root(self) -> None:
        with mock.patch.object(roots, 'wsl_roots', return_value=[]):
            found = roots.root_for_origin('windows')

        self.assertIsNotNone(found)
        self.assertEqual(found.origin, 'windows')

    def test_unknown_origin_is_refused_not_a_fallback(self) -> None:
        with mock.patch.object(roots, 'wsl_roots', return_value=[_fake_wsl_root()]):
            self.assertIsNone(roots.root_for_origin('nope'))

    def test_non_str_origin_is_refused(self) -> None:
        with mock.patch.object(roots, 'wsl_roots', return_value=[]):
            self.assertIsNone(roots.root_for_origin(5))


if __name__ == '__main__':
    unittest.main()
