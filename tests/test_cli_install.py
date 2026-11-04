"""Tests for reading the installed Claude Code version from the last self-update's result."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import cli_install
from agent_monitor_for_claude.cli_install import installed_version, parse_version
from agent_monitor_for_claude.paths import local_root

_SUCCESS = {
    'timestamp': '2026-11-04T08:01:47.741Z', 'path': 'native', 'outcome': 'success', 'status': 'success',
    'version_from': '2.1.317', 'version_to': '2.1.318', 'error_code': None,
}


class InstalledVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        self._file = Path(self._temp.name) / '.last-update-result.json'
        # The answer is cached per file for a while; every test starts empty.
        patcher = mock.patch.object(cli_install, '_versions', {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write(self, content: object) -> None:
        self._file.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')

    def test_a_successful_update_names_the_installed_version(self) -> None:
        self._write(_SUCCESS)
        self.assertEqual(installed_version(local_root()), '2.1.318')

    def test_a_failed_update_names_none(self) -> None:
        # The target of a failed update was never installed.
        self._write({**_SUCCESS, 'outcome': 'failure', 'status': 'failure', 'error_code': 'EACCES'})
        self.assertIsNone(installed_version(local_root()))

    def test_either_outcome_field_alone_is_enough(self) -> None:
        for missing in ('outcome', 'status'):
            with self.subTest(missing=missing):
                cli_install._versions.clear()
                self._write({key: value for key, value in _SUCCESS.items() if key != missing})
                self.assertEqual(installed_version(local_root()), '2.1.318')

    def test_a_missing_unreadable_or_reshaped_file_names_none(self) -> None:
        cases = {
            'missing': None,
            'not json': '{ not json',
            'not an object': ['2.1.318'],
            'no target': {key: value for key, value in _SUCCESS.items() if key != 'version_to'},
            'malformed target': {**_SUCCESS, 'version_to': 'latest'},
            'mistyped target': {**_SUCCESS, 'version_to': 2.1},
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                cli_install._versions.clear()
                if content is None:
                    self._file.unlink(missing_ok=True)
                else:
                    self._write(content)
                self.assertIsNone(installed_version(local_root()))

    def test_the_file_is_read_again_only_after_the_refresh_interval(self) -> None:
        self._write(_SUCCESS)
        with mock.patch.object(cli_install.time, 'monotonic', return_value=1000.0):
            self.assertEqual(installed_version(local_root()), '2.1.318')
        self._write({**_SUCCESS, 'version_to': '2.1.319'})

        with mock.patch.object(cli_install.time, 'monotonic', return_value=1000.0 + cli_install._REFRESH_SECONDS - 1):
            self.assertEqual(installed_version(local_root()), '2.1.318')
        with mock.patch.object(cli_install.time, 'monotonic', return_value=1000.0 + cli_install._REFRESH_SECONDS + 1):
            self.assertEqual(installed_version(local_root()), '2.1.319')


class ParseVersionTest(unittest.TestCase):
    def test_accepts_release_versions(self) -> None:
        for value in ('2.1.318', '1.0', '10.20.30.40'):
            with self.subTest(value=value):
                self.assertEqual(parse_version(value), value)

    def test_rejects_anything_else(self) -> None:
        for value in (None, '', '2', '2.1.318-beta', 'v2.1.318', '2..1', '1.2.3.4.5', '1234567.1', 2.1, True, ['2.1.318']):
            with self.subTest(value=value):
                self.assertIsNone(parse_version(value))


if __name__ == '__main__':
    unittest.main()
