"""
Tests for the encapsulated session content search.

The search is the one path that reads conversation text, so alongside the
functional cases these tests guard its two boundaries: it reports **only session
ids** (never content), and every read is **confined to** ``projects/`` (a crafted
id or cwd cannot escape it).
"""
from __future__ import annotations

import os
import tempfile
import unittest

from agent_monitor_for_claude import search
from agent_monitor_for_claude.paths import config_dir, transcript_path

_CWD = 'c:\\Temp\\search-proj'


class SearchEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write(self, session_id: str, cwd: str, text: str, mtime: float | None = None) -> None:
        path = transcript_path(session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def _run(self, query: object, sessions: object, options: object = None, should_cancel=None) -> list[tuple]:
        """Run a search synchronously, collecting every update it reports."""
        updates: list[tuple] = []

        def on_update(processed: int, total: int, matches: list[str], done: bool, error: bool) -> None:
            updates.append((processed, total, list(matches), done, error))

        search.run_search(query, sessions, options or {}, on_update, should_cancel)
        return updates

    def _matched_ids(self, updates: list[tuple]) -> list[str]:
        ids: list[str] = []
        for update in updates:
            ids.extend(update[2])
        return ids

    def _errored(self, updates: list[tuple]) -> bool:
        return any(update[4] for update in updates)

    def _ref(self, session_id: str, cwd: str = _CWD) -> dict[str, str]:
        return {'session_id': session_id, 'cwd': cwd}


class SearchTest(SearchEnvTest):
    def test_finds_a_session_by_content(self) -> None:
        self._write('id-a', _CWD, 'the quick brown fox')
        self._write('id-b', _CWD, 'nothing relevant here')

        updates = self._run('brown', [self._ref('id-a'), self._ref('id-b')])

        self.assertEqual(self._matched_ids(updates), ['id-a'])
        self.assertTrue(updates[-1][3], 'a final done update must always arrive')

    def test_matches_case_insensitively(self) -> None:
        self._write('id-u', _CWD, 'Grüße von der Straße')

        # Case-insensitive by default and Unicode-aware (Ü matches ü, S matches s).
        self.assertEqual(self._matched_ids(self._run('GRÜßE', [self._ref('id-u')])), ['id-u'])
        self.assertEqual(self._matched_ids(self._run('straße', [self._ref('id-u')])), ['id-u'])

    def test_reports_no_match_when_absent(self) -> None:
        self._write('id-a', _CWD, 'hello world')

        self.assertEqual(self._matched_ids(self._run('absent', [self._ref('id-a')])), [])

    def test_blank_oversized_or_non_string_query_matches_nothing(self) -> None:
        self._write('id-a', _CWD, 'hello world')
        refs = [self._ref('id-a')]

        for query in ('', '   ', 'x' * 500, None, 123):
            self.assertEqual(self._matched_ids(self._run(query, refs)), [])

    def test_invalid_sessions_argument_matches_nothing(self) -> None:
        self.assertEqual(self._matched_ids(self._run('x', None)), [])
        self.assertEqual(self._matched_ids(self._run('x', 'not-a-list')), [])
        self.assertEqual(self._matched_ids(self._run('x', [])), [])

    def test_missing_transcript_is_skipped(self) -> None:
        # No file was written for this id, so there is nothing to read.
        self.assertEqual(self._matched_ids(self._run('x', [self._ref('ghost')])), [])

    def test_reports_newest_session_first(self) -> None:
        self._write('older', _CWD, 'match', mtime=1000)
        self._write('newer', _CWD, 'match', mtime=5000)

        ids = self._matched_ids(self._run('match', [self._ref('older'), self._ref('newer')]))

        self.assertEqual(ids, ['newer', 'older'])

    def test_a_cancelled_search_reports_nothing(self) -> None:
        self._write('id-a', _CWD, 'match')

        updates = self._run('match', [self._ref('id-a')], should_cancel=lambda: True)

        self.assertEqual(self._matched_ids(updates), [])

    def test_progress_totals_reflect_the_scope(self) -> None:
        self._write('id-a', _CWD, 'match')
        self._write('id-b', _CWD, 'match')

        updates = self._run('match', [self._ref('id-a'), self._ref('id-b')])

        processed, total, _matches, done, _error = updates[-1]
        self.assertEqual(total, 2)
        self.assertEqual(processed, 2)
        self.assertTrue(done)


class SearchOptionsTest(SearchEnvTest):
    def test_match_case_option(self) -> None:
        self._write('id-a', _CWD, 'The quick brown Fox')
        ref = [self._ref('id-a')]

        self.assertEqual(self._matched_ids(self._run('fox', ref)), ['id-a'])
        self.assertEqual(self._matched_ids(self._run('fox', ref, {'match_case': True})), [])
        self.assertEqual(self._matched_ids(self._run('Fox', ref, {'match_case': True})), ['id-a'])

    def test_whole_word_option(self) -> None:
        self._write('id-a', _CWD, 'the foxes ran')
        ref = [self._ref('id-a')]

        # A substring hits 'foxes'; a whole-word 'fox' does not.
        self.assertEqual(self._matched_ids(self._run('fox', ref)), ['id-a'])
        self.assertEqual(self._matched_ids(self._run('fox', ref, {'whole_word': True})), [])
        self.assertEqual(self._matched_ids(self._run('foxes', ref, {'whole_word': True})), ['id-a'])

    def test_plain_mode_treats_the_query_literally(self) -> None:
        self._write('dot', _CWD, 'value a.b here')
        self._write('nodot', _CWD, 'value axb here')
        refs = [self._ref('dot'), self._ref('nodot')]

        # Without regex mode the '.' is a literal dot, not "any character".
        self.assertEqual(self._matched_ids(self._run('a.b', refs)), ['dot'])

    def test_regex_option(self) -> None:
        self._write('id-a', _CWD, 'order 12345 shipped')
        ref = [self._ref('id-a')]

        self.assertEqual(self._matched_ids(self._run(r'\d{5}', ref, {'use_regex': True})), ['id-a'])
        # The same pattern as a literal string does not match.
        self.assertEqual(self._matched_ids(self._run(r'\d{5}', ref)), [])

    def test_invalid_regex_reports_error_and_no_matches(self) -> None:
        self._write('id-a', _CWD, 'anything')
        ref = [self._ref('id-a')]

        errored = self._run('(', ref, {'use_regex': True})
        self.assertTrue(self._errored(errored))
        self.assertEqual(self._matched_ids(errored), [])

        # The same text as a literal (regex off) is fine - no error.
        self.assertFalse(self._errored(self._run('(', ref)))


class SearchBoundaryTest(SearchEnvTest):
    def test_reports_only_ids_never_content(self) -> None:
        self._write('id-a', _CWD, 'SECRET_BODY that surrounds the findme needle')

        batches: list[list[str]] = []

        def on_update(processed: int, total: int, matches: list[str], done: bool, error: bool) -> None:
            batches.append(list(matches))

        search.run_search('findme', [self._ref('id-a')], {}, on_update)

        reported = [value for batch in batches for value in batch]
        self.assertEqual(reported, ['id-a'])
        for value in reported:
            self.assertNotIn('SECRET_BODY', value)

    def test_path_traversal_is_confined_to_projects(self) -> None:
        # A file outside projects/ that a crafted id would resolve to via `..`.
        secret = config_dir() / 'outside-secret.jsonl'
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text('match', encoding='utf-8')

        refs = [self._ref('../../outside-secret')]

        self.assertEqual(self._matched_ids(self._run('match', refs)), [])


if __name__ == '__main__':
    unittest.main()
