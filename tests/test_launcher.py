"""
Tests for the Linux launcher script.

Only the parts that can be checked without opening a window: that the script is
executable at all, and that a checkout without a virtual environment is refused
with an explanation rather than a Python traceback.  The success path starts the
real application, so it is exercised by hand rather than here.

Skipped as a whole on Windows, where the script has no role.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux launcher')

_LAUNCHER = Path(__file__).resolve().parent.parent / 'agent-monitor-for-claude'


@_LINUX_ONLY
class LauncherTest(unittest.TestCase):
    def test_it_ships_executable(self) -> None:
        # The mode bit is the whole point of the file; losing it turns the
        # documented one-liner into "permission denied".
        self.assertTrue(_LAUNCHER.is_file())
        self.assertTrue(os.access(_LAUNCHER, os.X_OK))

    def test_it_is_posix_shell(self) -> None:
        self.assertTrue(_LAUNCHER.read_text(encoding='utf-8').startswith('#!/bin/sh\n'))
        result = subprocess.run(['sh', '-n', str(_LAUNCHER)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_checkout_without_a_virtual_environment_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            copy = Path(base) / _LAUNCHER.name
            shutil.copy2(_LAUNCHER, copy)

            result = subprocess.run([str(copy)], capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn('No virtual environment', result.stderr)
        self.assertIn('README.md', result.stderr)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
