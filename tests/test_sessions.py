"""Tests for the session registry inventory (defensive parsing)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import sessions as sessions_mod
from agent_monitor_for_claude.paths import SessionRoot, local_root
from agent_monitor_for_claude.sessions import list_sessions


class SessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        self._sessions = Path(self._temp.name) / 'sessions'
        self._sessions.mkdir()
        self.root = local_root()

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
        records = list_sessions(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['session_id'], 'abc-def')
        self.assertEqual(records[0]['pid'], 1234)
        self.assertEqual(records[0]['name'], 'proj-a1')
        self.assertEqual(records[0]['entrypoint'], 'claude-vscode')
        self.assertEqual(records[0]['origin'], 'local')
        self.assertIsNone(records[0]['origin_label'])

    def test_parses_native_status_and_waiting_for(self) -> None:
        self._write('5.json', {
            'pid': 5, 'sessionId': 's', 'cwd': 'd:\\x',
            'status': 'waiting', 'waitingFor': 'permission prompt',
        })
        record = list_sessions(self.root)[0]
        self.assertEqual(record['native_status'], 'waiting')
        self.assertEqual(record['waiting_for'], 'permission prompt')

    def test_missing_native_status_and_waiting_for_are_none(self) -> None:
        self._write('6.json', {'pid': 6, 'sessionId': 's', 'cwd': 'd:\\x', 'status': 42, 'waitingFor': []})
        record = list_sessions(self.root)[0]
        self.assertIsNone(record['native_status'])
        self.assertIsNone(record['waiting_for'])

    def test_parses_the_background_job_id(self) -> None:
        # Only a `claude --bg` session carries one, and it is that session's only
        # handle: it runs under the daemon with no window to raise.
        self._write('1234.json', {
            'pid': 1234, 'sessionId': 'abc-def', 'cwd': 'd:\Dev\proj',
            'kind': 'bg', 'entrypoint': 'cli', 'jobId': '95dc95aa',
        })
        record = list_sessions(self.root)[0]
        self.assertEqual(record['kind'], 'bg')
        self.assertEqual(record['job_id'], '95dc95aa')

    def test_missing_or_mistyped_job_id_is_none(self) -> None:
        self._write('1234.json', {'pid': 1234, 'sessionId': 'abc-def', 'cwd': 'd:\Dev\proj'})
        self._write('5678.json', {'pid': 5678, 'sessionId': 'ghi-jkl', 'cwd': 'd:\Dev\proj', 'jobId': 17})
        self._write('9012.json', {'pid': 9012, 'sessionId': 'mno-pqr', 'cwd': 'd:\Dev\proj', 'jobId': ''})
        for record in list_sessions(self.root):
            self.assertIsNone(record['job_id'])

    def test_a_named_bridge_session_means_remote_control_is_on(self) -> None:
        self._write('1234.json', {'pid': 1234, 'sessionId': 'abc-def', 'cwd': 'd:\\x', 'bridgeSessionId': 'session_0185EZpWjqviNrxGgSiuWyPo'})
        record = list_sessions(self.root)[0]
        self.assertIs(record['remote_control'], True)
        # Only the flag is taken; the cloud session id never leaves the parser.
        self.assertNotIn('session_0185EZpWjqviNrxGgSiuWyPo', json.dumps(record))

    def test_missing_null_or_mistyped_bridge_session_is_off(self) -> None:
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x'})
        self._write('2.json', {'pid': 2, 'sessionId': 'b', 'cwd': 'd:\\x', 'bridgeSessionId': None})
        self._write('3.json', {'pid': 3, 'sessionId': 'c', 'cwd': 'd:\\x', 'bridgeSessionId': ''})
        self._write('4.json', {'pid': 4, 'sessionId': 'd', 'cwd': 'd:\\x', 'bridgeSessionId': True})
        records = list_sessions(self.root)
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertIs(record['remote_control'], False)

    def test_name_defaults_to_session_prefix(self) -> None:
        self._write('9.json', {'pid': 9, 'sessionId': 'abcdefghij', 'cwd': 'd:\\x'})
        records = list_sessions(self.root)
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
        # An empty cwd is likewise unusable (a blank-named project panel, no
        # transcript path), so it must be dropped too - not half-tolerated.
        self._write('g.json', {'pid': 11, 'sessionId': 's', 'cwd': ''})
        self.assertEqual(list_sessions(self.root), [])

    def test_parses_proc_start_and_started_at(self) -> None:
        self._write('7.json', {'pid': 7, 'sessionId': 's', 'cwd': 'd:\\x', 'procStart': '639193663837674260', 'startedAt': 1783762384512})
        record = list_sessions(self.root)[0]
        self.assertEqual(record['proc_start_ticks'], 639193663837674260)
        self.assertEqual(record['started_at'], 1783762384512.0)

    def test_invalid_proc_start_and_started_at_ignored(self) -> None:
        self._write('8.json', {'pid': 8, 'sessionId': 's', 'cwd': 'd:\\x', 'procStart': 'garbage', 'startedAt': 'nope'})
        record = list_sessions(self.root)[0]
        self.assertIsNone(record['proc_start_ticks'])
        self.assertIsNone(record['started_at'])

    def test_parses_when_the_native_status_last_changed(self) -> None:
        self._write('9.json', {'pid': 9, 'sessionId': 's', 'cwd': 'd:\\x', 'status': 'busy', 'statusUpdatedAt': 1793790939696})
        self.assertEqual(list_sessions(self.root)[0]['native_status_since'], 1793790939696.0)

    def test_a_missing_or_mistyped_status_change_time_is_none(self) -> None:
        # An older Claude Code writes no such field; a mistyped one must not
        # reach the UI as a duration.
        for index, value in enumerate((None, 'yesterday', True, -5, [1])):
            with self.subTest(value=value):
                record = {'pid': 20 + index, 'sessionId': 's', 'cwd': 'd:\\x'}
                if value is not None:
                    record['statusUpdatedAt'] = value
                self._write(f'{20 + index}.json', record)
                parsed = next(item for item in list_sessions(self.root) if item['pid'] == 20 + index)
                self.assertIsNone(parsed['native_status_since'])

    def test_missing_directory_returns_empty(self) -> None:
        os.environ['CLAUDE_CONFIG_DIR'] = str(Path(self._temp.name) / 'does-not-exist')
        self.assertEqual(list_sessions(local_root()), [])

    def _settle(self, name: str) -> None:
        """Date *name* back past the recent-write guard, as a file written a while ago."""
        stamp = time.time() - 60
        os.utime(self._sessions / name, (stamp, stamp))

    def _list_counting_reads(self) -> tuple[list[dict[str, object]], int]:
        with mock.patch.object(sessions_mod, '_read_json', wraps=sessions_mod._read_json) as read_json:
            records = list_sessions(self.root)
        return records, read_json.call_count

    def test_an_unchanged_file_is_not_read_again(self) -> None:
        # Hundreds of ended sessions' records can sit in a registry read over a
        # slow share on every poll; only a changed file may cost a read.
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x'})
        self._write('2.json', {'not': 'a record'})
        self._settle('1.json')
        self._settle('2.json')
        list_sessions(self.root)

        records, reads = self._list_counting_reads()

        self.assertEqual(reads, 0)
        self.assertEqual([record['session_id'] for record in records], ['a'])

    def test_a_changed_file_is_read_again(self) -> None:
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x', 'status': 'busy'})
        self._settle('1.json')
        list_sessions(self.root)

        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x', 'status': 'waiting'})
        self._settle('1.json')
        records, reads = self._list_counting_reads()

        self.assertEqual(reads, 1)
        self.assertEqual(records[0]['native_status'], 'waiting')

    def test_a_recently_written_file_is_read_again_even_with_the_same_time_and_size(self) -> None:
        # Two rewrites within one timestamp tick leave time and size alike
        # (busy -> idle keeps the length); the second must still be seen.
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x', 'status': 'busy'})
        stat_before = (self._sessions / '1.json').stat()
        list_sessions(self.root)

        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x', 'status': 'idle'})
        os.utime(self._sessions / '1.json', ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        records, reads = self._list_counting_reads()

        self.assertEqual(reads, 1)
        self.assertEqual(records[0]['native_status'], 'idle')

    def test_a_returned_record_can_be_changed_without_touching_the_cache(self) -> None:
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x'})
        self._settle('1.json')
        list_sessions(self.root)[0]['cwd'] = 'd:\\elsewhere'

        self.assertEqual(list_sessions(self.root)[0]['cwd'], 'd:\\x')

    def test_a_removed_file_leaves_the_listing(self) -> None:
        self._write('1.json', {'pid': 1, 'sessionId': 'a', 'cwd': 'd:\\x'})
        self._write('2.json', {'pid': 2, 'sessionId': 'b', 'cwd': 'd:\\x'})
        self._settle('1.json')
        self._settle('2.json')
        list_sessions(self.root)

        (self._sessions / '2.json').unlink()

        self.assertEqual([record['session_id'] for record in list_sessions(self.root)], ['a'])

    def test_origin_and_origin_label_come_from_a_wsl_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(base), proc_dir=None, claude_temp_dir=Path(base))
            (Path(base) / 'sessions').mkdir()
            (Path(base) / 'sessions' / '1.json').write_text(
                json.dumps({'pid': 1, 'sessionId': 's', 'cwd': '/home/dev/proj'}), encoding='utf-8')

            record = list_sessions(wsl_root)[0]

        self.assertEqual(record['origin'], 'wsl:U')
        self.assertEqual(record['origin_label'], 'U')


if __name__ == '__main__':
    unittest.main()
