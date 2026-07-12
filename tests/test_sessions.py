"""Tests for the session registry inventory (defensive parsing)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_monitor_for_claude.sessions import list_sessions


class SessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        self._sessions = Path(self._temp.name) / 'sessions'
        self._sessions.mkdir()

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write(self, name: str, content: object) -> None:
        text = content if isinstance(content, str) else json.dumps(content)
        (self._sessions / name).write_text(text, encoding='utf-8')

    def test_parses_valid_record(self) -> None:
        self._write('1234.json', {
            'pid': 1234, 'sessionId': 'abc-def', 'cwd': 'd:\\Dev\\proj',
            'name': 'proj-a1', 'kind': 'interactive', 'entrypoint': 'claude-vscode', 'startedAt': 42,
        })
        records = list_sessions()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['session_id'], 'abc-def')
        self.assertEqual(records[0]['pid'], 1234)
        self.assertEqual(records[0]['name'], 'proj-a1')
        self.assertEqual(records[0]['entrypoint'], 'claude-vscode')

    def test_parses_native_status_and_waiting_for(self) -> None:
        self._write('5.json', {
            'pid': 5, 'sessionId': 's', 'cwd': 'd:\\x',
            'status': 'waiting', 'waitingFor': 'permission prompt',
        })
        record = list_sessions()[0]
        self.assertEqual(record['native_status'], 'waiting')
        self.assertEqual(record['waiting_for'], 'permission prompt')

    def test_missing_native_status_and_waiting_for_are_none(self) -> None:
        self._write('6.json', {'pid': 6, 'sessionId': 's', 'cwd': 'd:\\x', 'status': 42, 'waitingFor': []})
        record = list_sessions()[0]
        self.assertIsNone(record['native_status'])
        self.assertIsNone(record['waiting_for'])

    def test_name_defaults_to_session_prefix(self) -> None:
        self._write('9.json', {'pid': 9, 'sessionId': 'abcdefghij', 'cwd': 'd:\\x'})
        records = list_sessions()
        self.assertEqual(records[0]['name'], 'abcdefgh')

    def test_skips_invalid_records(self) -> None:
        self._write('a.json', {'pid': 'not-int', 'sessionId': 's', 'cwd': 'd:\\x'})
        self._write('b.json', {'sessionId': 's', 'cwd': 'd:\\x'})
        self._write('c.json', {})
        self._write('d.json', '{ not valid json')
        self._write('e.json', {'pid': True, 'sessionId': 's', 'cwd': 'd:\\x'})
        # An empty sessionId is not a usable required field - it must be dropped,
        # not pass the guard and yield a blank name/no transcript.
        self._write('f.json', {'pid': 10, 'sessionId': '', 'cwd': 'd:\\x'})
        self.assertEqual(list_sessions(), [])

    def test_parses_proc_start_and_started_at(self) -> None:
        self._write('7.json', {'pid': 7, 'sessionId': 's', 'cwd': 'd:\\x', 'procStart': '639193663837674260', 'startedAt': 1783762384512})
        record = list_sessions()[0]
        self.assertEqual(record['proc_start_ticks'], 639193663837674260)
        self.assertEqual(record['started_at'], 1783762384512.0)

    def test_invalid_proc_start_and_started_at_ignored(self) -> None:
        self._write('8.json', {'pid': 8, 'sessionId': 's', 'cwd': 'd:\\x', 'procStart': 'garbage', 'startedAt': 'nope'})
        record = list_sessions()[0]
        self.assertIsNone(record['proc_start_ticks'])
        self.assertIsNone(record['started_at'])

    def test_missing_directory_returns_empty(self) -> None:
        os.environ['CLAUDE_CONFIG_DIR'] = str(Path(self._temp.name) / 'does-not-exist')
        self.assertEqual(list_sessions(), [])


if __name__ == '__main__':
    unittest.main()
