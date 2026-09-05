"""Tests for path and slug derivation."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude.paths import (
    SessionRoot, claude_temp_dir, cwd_to_slug, host_path, local_root, task_output_dir, transcript_path,
)

_WINDOWS_ONLY = unittest.skipUnless(sys.platform == 'win32', 'Windows host behaviour')
_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux host behaviour')


def _wsl_root() -> SessionRoot:
    """A WSL root as a Windows host sees it, for the layout and translation tests."""
    return SessionRoot(
        origin='wsl:Ubuntu', label='Ubuntu',
        config_dir=Path(r'\\wsl.localhost\Ubuntu\home\dev\.claude'),
        proc_dir=Path(r'\\wsl.localhost\Ubuntu\proc'),
        claude_temp_dir=Path(r'\\wsl.localhost\Ubuntu\tmp'),
    )


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
    def test_local_root_shape(self) -> None:
        root = local_root()
        self.assertEqual(root.origin, 'local')
        self.assertIsNone(root.label)
        self.assertIsNone(root.proc_dir)
        self.assertTrue(root.config_dir.name == '.claude' or 'CLAUDE_CONFIG_DIR' in os.environ)

    def test_local_root_temp_dir_is_named_for_this_system(self) -> None:
        # Windows' temp directory is already per-user, so Claude Code names its
        # tree plainly there; a POSIX /tmp is shared, so the owner's uid is
        # appended - reading the wrong one would look for task output and
        # scratchpads that are not there.
        name = local_root().claude_temp_dir.name
        self.assertEqual(name, 'claude' if sys.platform == 'win32' else f'claude-{os.getuid()}')

    def test_transcript_path_uses_root(self) -> None:
        root = _wsl_root()
        path = transcript_path(root, 'abc', '/home/dev/proj')
        self.assertEqual(path, root.config_dir / 'projects' / '-home-dev-proj' / 'abc.jsonl')

    def test_task_output_dir_uses_root_temp(self) -> None:
        root = _wsl_root()
        self.assertEqual(task_output_dir(root, 'abc', '/home/dev/proj'),
                         root.claude_temp_dir / '-home-dev-proj' / 'abc' / 'tasks')


class ClaudeTempDirTests(unittest.TestCase):
    """A foreign temp tree (a WSL distro's) has no uid to ask for, so it is resolved by looking."""

    def test_this_machines_own_temp_dir_is_never_listed(self) -> None:
        # The local root is rebuilt on every poll, so a directory listing here
        # would be a per-second scan of a temp folder holding thousands of files.
        with mock.patch.object(Path, 'glob', side_effect=AssertionError('the local temp dir must not be listed')):
            self.assertEqual(claude_temp_dir(Path(tempfile.gettempdir())), local_root().claude_temp_dir)

    def test_single_uid_directory_wins(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'claude-1000').mkdir()
            self.assertEqual(claude_temp_dir(Path(base)), Path(base) / 'claude-1000')

    def test_several_candidates_fall_back_to_the_plain_name(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'claude-1000').mkdir()
            (Path(base) / 'claude-1001').mkdir()
            self.assertEqual(claude_temp_dir(Path(base)), Path(base) / 'claude')

    def test_no_candidate_falls_back_to_the_plain_name(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(claude_temp_dir(Path(base)), Path(base) / 'claude')

    def test_unreadable_temp_dir_falls_back_rather_than_raising(self) -> None:
        self.assertEqual(claude_temp_dir(Path('does-not-exist')), Path('does-not-exist') / 'claude')


@_WINDOWS_ONLY
class HostPathOnWindowsTests(unittest.TestCase):
    """Only a Windows host can reach into a distro, so only there is a path rewritten."""

    def test_mnt_drive_becomes_a_drive_letter(self) -> None:
        self.assertEqual(host_path(_wsl_root(), '/mnt/c/Users/dev/out.log'), 'C:\\Users\\dev\\out.log')

    def test_distro_path_becomes_a_unc_path(self) -> None:
        self.assertEqual(host_path(_wsl_root(), '/home/dev/run.log'),
                         '\\\\wsl.localhost\\Ubuntu\\home\\dev\\run.log')

    def test_local_root_still_maps_a_mount_but_leaves_a_windows_path(self) -> None:
        self.assertEqual(host_path(local_root(), 'C:\\x\\y.log'), 'C:\\x\\y.log')
        self.assertEqual(host_path(local_root(), '/mnt/c/x/y.log'), 'C:\\x\\y.log')


@_LINUX_ONLY
class HostPathOnLinuxTests(unittest.TestCase):
    """On Linux every path a session reports is already one this machine can open."""

    def test_paths_pass_through_untouched(self) -> None:
        root = local_root()
        self.assertEqual(host_path(root, '/home/dev/run.log'), '/home/dev/run.log')
        self.assertEqual(host_path(root, 'relative/run.log'), 'relative/run.log')

    def test_a_real_mnt_directory_is_not_mistaken_for_a_windows_drive(self) -> None:
        # /mnt/c is an ordinary mount point here, not WSL's view of a drive;
        # rewriting it to "C:\..." would point at nothing.
        self.assertEqual(host_path(local_root(), '/mnt/c/x/y.log'), '/mnt/c/x/y.log')


if __name__ == '__main__':
    unittest.main()
