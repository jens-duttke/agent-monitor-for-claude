"""Tests for path and slug derivation."""
from __future__ import annotations

import unittest

from agent_monitor_for_claude.paths import cwd_to_slug


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


if __name__ == '__main__':
    unittest.main()
