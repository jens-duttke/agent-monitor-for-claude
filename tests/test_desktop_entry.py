"""
Tests for the Linux desktop entry and the script that installs it.

The entry's file name, its ``Icon`` and its ``StartupWMClass`` must all be the
program name the Linux backend gives the process - that one name is what pairs
the running window with its launcher icon - so they are checked against each
other here.  The install script is run for real against a temporary data
directory; it is skipped on Windows, where it has no role.
"""
from __future__ import annotations

import configparser
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux desktop entry')

_ROOT = Path(__file__).resolve().parent.parent
_LINUX_DIR = _ROOT / 'linux'
_NAME = 'agent-monitor-for-claude'
_ENTRY = _LINUX_DIR / f'{_NAME}.desktop'
_ICON = _LINUX_DIR / f'{_NAME}.png'
_SCRIPT = _LINUX_DIR / 'install-desktop-entry'
_LAUNCHER = _ROOT / _NAME


def _read_entry(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # desktop entry keys are case-sensitive
    parser.read(path, encoding='utf-8')

    return dict(parser['Desktop Entry'])


class DesktopEntryTest(unittest.TestCase):
    def test_names_the_program_icon_and_window_class_alike(self) -> None:
        entry = _read_entry(_ENTRY)

        self.assertEqual(entry['Type'], 'Application')
        self.assertEqual(entry['Icon'], _NAME)
        self.assertEqual(entry['StartupWMClass'], _NAME)
        self.assertEqual(entry['Exec'], _NAME)
        self.assertEqual(entry['Terminal'], 'false')

    def test_icon_is_a_256_pixel_png(self) -> None:
        header = _ICON.read_bytes()[:24]

        self.assertEqual(header[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', header[16:24]), (256, 256))


@_LINUX_ONLY
class ProgramNameTest(unittest.TestCase):
    def test_backend_names_the_process_after_the_entry(self) -> None:
        from agent_monitor_for_claude.platforms import linux

        self.assertEqual(linux._PROGRAM_NAME, _NAME)


@_LINUX_ONLY
class InstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_home = Path(self._tmp.name)
        self.entry = self.data_home / 'applications' / f'{_NAME}.desktop'
        self.icon = self.data_home / 'icons' / 'hicolor' / '256x256' / 'apps' / f'{_NAME}.png'

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, XDG_DATA_HOME=str(self.data_home))
        return subprocess.run([str(_SCRIPT), *args], capture_output=True, text=True, env=env, check=False)

    def test_it_ships_executable_posix_shell(self) -> None:
        self.assertTrue(os.access(_SCRIPT, os.X_OK))
        self.assertTrue(_SCRIPT.read_text(encoding='utf-8').startswith('#!/bin/sh\n'))
        result = subprocess.run(['sh', '-n', str(_SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installs_the_entry_pointing_at_this_checkouts_launcher(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.icon.read_bytes(), _ICON.read_bytes())

        installed = _read_entry(self.entry)
        self.assertEqual(installed['Exec'], f'"{_LAUNCHER}"')
        self.assertEqual({key: value for key, value in installed.items() if key != 'Exec'},
                         {key: value for key, value in _read_entry(_ENTRY).items() if key != 'Exec'})

    def test_remove_takes_both_files_out_again(self) -> None:
        self._run()
        result = self._run('--remove')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.entry.exists())
        self.assertFalse(self.icon.exists())

    def test_an_unknown_argument_writes_nothing(self) -> None:
        result = self._run('--bogus')

        self.assertEqual(result.returncode, 2)
        self.assertIn('Usage', result.stderr)
        self.assertEqual(list(self.data_home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
