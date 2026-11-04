"""Tests for the context-window lookup in Claude Code's cached model catalog."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import model_catalog
from agent_monitor_for_claude.model_catalog import context_window
from agent_monitor_for_claude.paths import local_root, model_catalog_dir


def _catalog(*models: dict) -> dict:
    return {'version': 2, 'catalog': {'surface': 'cc', 'config': {'id': 'cc', 'models': list(models)}}}


def _model(model_id: object, tokens: object) -> dict:
    return {'id': model_id, 'name': 'Name', 'notice': {'text': 'not read'}, 'runtime': {'max_input_tokens': tokens}}


class ContextWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        model_catalog._tables.clear()
        self.addCleanup(model_catalog._tables.clear)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write(self, name: str, data: object, mtime: float | None = None) -> Path:
        directory = model_catalog_dir(local_root())
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding='utf-8')
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_the_window_of_a_listed_model(self) -> None:
        self._write('a-cc.json', _catalog(_model('claude-opus-5-5', 1000000), _model('claude-haiku-4-5-20251001', 200000)))

        self.assertEqual(context_window(local_root(), 'claude-opus-5-5'), 1000000)
        self.assertEqual(context_window(local_root(), 'claude-haiku-4-5-20251001'), 200000)

    def test_a_tier_suffix_falls_back_to_the_base_id(self) -> None:
        self._write('a-cc.json', _catalog(_model('claude-opus-5-5', 1000000)))

        self.assertEqual(context_window(local_root(), 'claude-opus-5-5[1m]'), 1000000)

    def test_an_unknown_or_missing_model_has_no_window(self) -> None:
        self._write('a-cc.json', _catalog(_model('claude-opus-5-5', 1000000)))

        self.assertIsNone(context_window(local_root(), 'claude-unknown-1'))
        self.assertIsNone(context_window(local_root(), None))
        self.assertIsNone(context_window(local_root(), ''))

    def test_a_newer_file_overrides_an_older_one(self) -> None:
        self._write('old-cc.json', _catalog(_model('claude-opus-5-5', 200000)), mtime=1000.0)
        self._write('new-ccd.json', _catalog(_model('claude-opus-5-5', 1000000)), mtime=2000.0)

        self.assertEqual(context_window(local_root(), 'claude-opus-5-5'), 1000000)

    def test_malformed_entries_and_files_are_skipped(self) -> None:
        self._write('broken.json', '{not json')
        self._write('shape.json', {'catalog': {'config': {'models': 'nope'}}})
        self._write('a-cc.json', _catalog(_model('ok', 5000), _model('zero', 0), _model('flag', True), _model(7, 100),
                                          'not a model', {'id': 'no-runtime'}))

        self.assertEqual(context_window(local_root(), 'ok'), 5000)
        for model_id in ('zero', 'flag', 'no-runtime'):
            self.assertIsNone(context_window(local_root(), model_id))

    def test_no_catalog_directory_means_no_window(self) -> None:
        self.assertIsNone(context_window(local_root(), 'claude-opus-5-5'))

    def test_the_table_is_reread_only_after_the_refresh_interval(self) -> None:
        self._write('a-cc.json', _catalog(_model('m', 100)))
        with mock.patch.object(model_catalog.time, 'monotonic', return_value=1000.0):
            self.assertEqual(context_window(local_root(), 'm'), 100)
        self._write('a-cc.json', _catalog(_model('m', 200)))

        with mock.patch.object(model_catalog.time, 'monotonic', return_value=1000.0 + model_catalog._REFRESH_SECONDS - 1):
            self.assertEqual(context_window(local_root(), 'm'), 100)
        with mock.patch.object(model_catalog.time, 'monotonic', return_value=1000.0 + model_catalog._REFRESH_SECONDS):
            self.assertEqual(context_window(local_root(), 'm'), 200)


if __name__ == '__main__':
    unittest.main()
