"""Tests for path and slug derivation."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from agent_monitor_for_claude.paths import SessionRoot, cwd_to_slug, task_output_dir, transcript_path, windows_root, wsl_path_to_windows


class SlugTest(unittest.TestCase):
    def test_windows_drive_path(self) -> None:
        self.assertEqual(cwd_to_slug('d:\\WebDev\\oku3d-app'), 'd--WebDev-oku3d-app')

    def test_preserves_existing_hyphens(self) -> None:
        self.assertEqual(cwd_to_slug('d:\\PythonDev\\claude-usage-tray'), 'd--PythonDev-claude-usage-tray')

    def test_forward_slashes(self) -> None:
        self.assertEqual(cwd_to_slug('c:/Temp/x'), 'c--Temp-x')

    def test_mixed_separators(self) -> None:
        self.assertEqual(cwd_to_slug('c:\\a/b'), 'c--a-b')

    def test_dot_in_path_segment(self) -> None:
        # Claude Code replaces dots with hyphens too, so a folder like HexEd.it
        # maps to ...HexEd-it on disk - the previous separator-only rule missed
        # this and mislocated the transcript for any dotted project path.
        self.assertEqual(cwd_to_slug('d:\\WebDev\\HexEd.it'), 'd--WebDev-HexEd-it')
        self.assertEqual(cwd_to_slug('d:\\WebDev\\duttke.de-next'), 'd--WebDev-duttke-de-next')

    def test_replaces_any_non_alphanumeric(self) -> None:
        # Spaces and other punctuation collapse to a single hyphen each, never
        # collapsed together, mirroring Claude Code's own slug encoding.
        self.assertEqual(cwd_to_slug('c:\\My Project (v2)'), 'c--My-Project--v2-')


class SessionRootTests(unittest.TestCase):
    def _wsl_root(self):
        return SessionRoot(origin='wsl:Ubuntu', label='Ubuntu',
                           config_dir=Path(r'\\wsl.localhost\Ubuntu\home\dev\.claude'),
                           proc_dir=Path(r'\\wsl.localhost\Ubuntu\proc'),
                           temp_dir=Path(r'\\wsl.localhost\Ubuntu\tmp'))

    def test_windows_root_shape(self):
        root = windows_root()
        self.assertEqual(root.origin, 'windows')
        self.assertIsNone(root.label)
        self.assertIsNone(root.proc_dir)
        self.assertTrue(root.config_dir.name == '.claude' or 'CLAUDE_CONFIG_DIR' in os.environ)

    def test_transcript_path_uses_root(self):
        root = self._wsl_root()
        path = transcript_path(root, 'abc', '/home/dev/proj')
        self.assertEqual(path, root.config_dir / 'projects' / '-home-dev-proj' / 'abc.jsonl')

    def test_task_output_dir_uses_root_temp(self):
        root = self._wsl_root()
        self.assertEqual(task_output_dir(root, 'abc', '/home/dev/proj'),
                         root.temp_dir / 'claude' / '-home-dev-proj' / 'abc' / 'tasks')

    def test_wsl_path_to_windows_mnt(self):
        self.assertEqual(wsl_path_to_windows(self._wsl_root(), '/mnt/c/Users/dev/out.log'), 'C:\\Users\\dev\\out.log')

    def test_wsl_path_to_windows_posix(self):
        self.assertEqual(wsl_path_to_windows(self._wsl_root(), '/home/dev/run.log'),
                         '\\\\wsl.localhost\\Ubuntu\\home\\dev\\run.log')

    def test_wsl_path_to_windows_passthrough_on_windows_root(self):
        self.assertEqual(wsl_path_to_windows(windows_root(), 'C:\\x\\y.log'), 'C:\\x\\y.log')
        self.assertEqual(wsl_path_to_windows(windows_root(), '/mnt/c/x/y.log'), 'C:\\x\\y.log')


if __name__ == '__main__':
    unittest.main()
