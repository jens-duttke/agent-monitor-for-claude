"""
Tests for the encapsulated session content search.

The search is the one path that reads conversation text and shows some of it, so
alongside the functional cases these tests guard its boundaries: what it reports
is **bounded** (a hit count plus a few short excerpts, never a transcript dump),
matching runs on **readable text** rather than on control metadata, and every
read is **confined to** ``projects/`` (a crafted id or cwd cannot escape it).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import search
from agent_monitor_for_claude.paths import SessionRoot, config_dir, transcript_path, local_root

_CWD = 'c:\\Temp\\search-proj'

# A syntactically valid session id, standing in for a real Claude Code session
# uuid on the fake WSL root the SearchOriginTest cases below build.
WSL_SID = '8d49a52c-4ac7-43ec-a7e1-773be955bf59'


def _windows_only_root(origin: object) -> SessionRoot | None:
    """Stand in for ``roots.root_for_origin``, resolving only the ``'local'`` origin.

    Pinned onto ``search.root_for_origin`` for every ``SearchEnvTest`` case: a
    plain ref with no explicit ``'origin'`` key defaults to ``'local'`` (see
    ``search._valid_refs``), which would otherwise resolve through the real
    ``roots.root_for_origin`` - and its live WSL discovery via
    ``roots.session_roots()`` - on every single search in this file. Origin
    resolution itself, including a WSL origin and an unknown one, is covered by
    the dedicated ``SearchOriginTest`` cases below, which patch
    ``search.root_for_origin`` again for their own narrower scope.
    """
    return local_root() if origin == 'local' else None


class SearchEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name

        roots_patcher = mock.patch.object(search, 'root_for_origin', side_effect=_windows_only_root)
        roots_patcher.start()
        self.addCleanup(roots_patcher.stop)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write(self, session_id: str, cwd: str, text: str, mtime: float | None = None) -> None:
        path = transcript_path(local_root(), session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def _run(self, query: object, sessions: object, options: object = None, should_cancel=None) -> list[tuple]:
        """Run a search synchronously, collecting every update it reports."""
        updates: list[tuple] = []

        def on_update(processed: int, total: int, matches: list[dict], done: bool, error: bool) -> None:
            updates.append((processed, total, list(matches), done, error))

        search.run_search(query, sessions, options or {}, on_update, should_cancel)
        return updates

    def _matched_ids(self, updates: list[tuple]) -> list[str]:
        ids: list[str] = []
        for update in updates:
            ids.extend(match['session_id'] for match in update[2])
        return ids

    def _matches(self, updates: list[tuple]) -> list[dict]:
        found: list[dict] = []
        for update in updates:
            found.extend(update[2])
        return found

    def _entry(self, text: str, entry_type: str = 'user') -> str:
        """One transcript line in the shape Claude Code writes, carrying *text*."""
        return json.dumps({
            'type': entry_type, 'uuid': 'e7c1c0de-0000-4000-8000-000000000000',
            'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': _CWD,
            'message': {'role': entry_type, 'content': [{'type': 'text', 'text': text}]},
        }) + '\n'

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

    def test_blank_or_non_string_query_matches_nothing(self) -> None:
        self._write('id-a', _CWD, 'hello world')
        refs = [self._ref('id-a')]

        for query in ('', '   ', None, 123):
            self.assertEqual(self._matched_ids(self._run(query, refs)), [])

    def test_over_long_query_is_rejected_even_when_present(self) -> None:
        # A query longer than the cap must match nothing even when its exact text
        # IS in the content - proving the length guard, not the text's absence.
        long_text = 'y' * (search._MAX_QUERY_LEN + 1)
        self._write('id-long', _CWD, long_text)

        result = self._run(long_text, [self._ref('id-long')])

        self.assertEqual(self._matched_ids(result), [])
        self.assertFalse(self._errored(result))   # over-long is "no filter", not an error

    def test_max_length_query_is_accepted(self) -> None:
        # The boundary is inclusive: exactly the cap is a valid query and matches.
        boundary_text = 'z' * search._MAX_QUERY_LEN
        self._write('id-max', _CWD, boundary_text)

        self.assertEqual(self._matched_ids(self._run(boundary_text, [self._ref('id-max')])), ['id-max'])

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


class SearchHitsTest(SearchEnvTest):
    """What a match reports beyond its id: how many hits, and where they are."""

    def test_counts_every_hit_in_the_session(self) -> None:
        self._write('id-a', _CWD, self._entry('needle here') + self._entry('needle and needle again'))

        matches = self._matches(self._run('needle', [self._ref('id-a')]))

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['count'], 3)

    def test_an_excerpt_carries_the_text_either_side_of_the_hit(self) -> None:
        self._write('id-a', _CWD, self._entry('left side needle right side'))

        snippet = self._matches(self._run('needle', [self._ref('id-a')]))[0]['snippets'][0]

        self.assertEqual(snippet['match'], 'needle')
        self.assertTrue(snippet['before'].endswith('left side '))
        self.assertTrue(snippet['after'].startswith(' right side'))

    def test_an_excerpt_names_where_it_came_from(self) -> None:
        assistant = json.dumps({
            'type': 'assistant',
            'message': {'content': [
                {'type': 'thinking', 'thinking': 'needle in thought'},
                {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'echo needle'}},
            ]},
        }) + '\n'
        self._write('id-a', _CWD, assistant)

        snippets = self._matches(self._run('needle', [self._ref('id-a')]))[0]['snippets']
        kinds = [(snippet['kind'], snippet.get('tool')) for snippet in snippets]

        self.assertIn(('thinking', None), kinds)
        self.assertIn(('tool_use', 'Bash'), kinds)

    def test_a_tool_result_is_searched_and_labelled(self) -> None:
        line = json.dumps({
            'type': 'user',
            'message': {'content': [{'type': 'tool_result', 'content': 'output with needle inside'}]},
        }) + '\n'
        self._write('id-a', _CWD, line)

        snippets = self._matches(self._run('needle', [self._ref('id-a')]))[0]['snippets']

        self.assertEqual([snippet['kind'] for snippet in snippets], ['tool_result'])

    def test_an_excerpt_is_flattened_to_one_line(self) -> None:
        self._write('id-a', _CWD, self._entry('first line\n\tindented needle here'))

        snippet = self._matches(self._run('needle', [self._ref('id-a')]))[0]['snippets'][0]

        self.assertNotIn('\n', snippet['before'] + snippet['match'] + snippet['after'])
        self.assertNotIn('\t', snippet['before'])
        # Context is cut at the hit's own line, so the preceding line is not in it.
        self.assertNotIn('first line', snippet['before'])

    def test_a_hit_at_the_start_of_its_line_is_not_marked_clipped(self) -> None:
        # The clip mark sits at the excerpt's edge and reads as "this line goes
        # on", so an earlier line must not put one in front of a hit that starts
        # its own line - only text missing from the hit's own line may.
        self._write('id-a', _CWD, self._entry('a preceding line\nneedle starts this line'))

        snippet = self._matches(self._run('needle', [self._ref('id-a')]))[0]['snippets'][0]

        self.assertNotIn('clipped_before', snippet)
        self.assertNotIn('clipped_after', snippet)
        self.assertEqual(snippet['before'], '')

    def test_a_nested_tool_input_is_searched(self) -> None:
        # A tool's arguments are its own shape - MultiEdit nests its strings in a
        # list of objects - so the input is walked rather than named field by field.
        line = json.dumps({
            'type': 'assistant',
            'message': {'content': [{
                'type': 'tool_use', 'name': 'MultiEdit',
                'input': {'edits': [{'old_string': 'before', 'new_string': 'needle inside'}]},
            }]},
        })
        self._write('id-a', _CWD, line)

        match = self._matches(self._run('needle', [self._ref('id-a')]))[0]

        self.assertEqual(match['count'], 1)
        self.assertEqual(match['snippets'][0]['tool'], 'MultiEdit')

    def test_a_malformed_entry_neither_matches_nor_raises(self) -> None:
        # Unversioned internals: a renamed or mistyped field must degrade to
        # "nothing to search here", never to a crash or a metadata hit.
        for entry in (
            {'type': 'user'},                                        # no message at all
            {'type': 'user', 'message': 'needle'},                   # message not an object
            {'type': 'user', 'message': {'content': 42}},            # content not text or list
            {'type': 'user', 'message': {'content': [None, 7]}},     # blocks not objects
            {'type': 'user', 'message': {'content': [{'type': 'text'}]}},          # text block, no text
            {'type': 'user', 'message': {'content': [{'type': 'unknown_kind', 'text': 'needle'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 5, 'input': None}]}},
        ):
            with self.subTest(entry=entry):
                self._write('id-a', _CWD, json.dumps(entry))

                self.assertEqual(self._matched_ids(self._run('needle', [self._ref('id-a')])), [])

    def test_a_line_that_is_not_json_is_still_searched(self) -> None:
        # A format change must degrade to searching the plain text, not to
        # finding nothing at all.
        self._write('id-a', _CWD, 'plain text with a needle in it\n')

        matches = self._matches(self._run('needle', [self._ref('id-a')]))

        self.assertEqual(matches[0]['count'], 1)
        self.assertEqual(matches[0]['snippets'][0]['kind'], 'raw')


class SearchReadableTextTest(SearchEnvTest):
    """Matching runs on what somebody wrote or read, not on control metadata."""

    def test_metadata_fields_do_not_match(self) -> None:
        # The uuid, the timestamp and the project path sit on every single line;
        # matching them would report a hit in every session that ever ran.
        self._write('id-a', _CWD, self._entry('nothing of interest'))
        refs = [self._ref('id-a')]

        self.assertEqual(self._matched_ids(self._run('e7c1c0de', refs)), [])
        self.assertEqual(self._matched_ids(self._run('2026-01-01', refs)), [])
        self.assertEqual(self._matched_ids(self._run('search-proj', refs)), [])

    def test_message_text_matches(self) -> None:
        self._write('id-a', _CWD, self._entry('nothing of interest'))

        self.assertEqual(self._matched_ids(self._run('of interest', [self._ref('id-a')])), ['id-a'])

    def test_a_json_escaped_character_is_still_found(self) -> None:
        # The prefilter runs against the raw line, where a quote and a non-ASCII
        # character are written differently than they read once parsed. Missing
        # one would hide the hit entirely, so each is checked.
        self._write('id-a', _CWD, self._entry('sie sagte "uber den Fluss" und grusste über alles'))
        refs = [self._ref('id-a')]

        self.assertEqual(self._matched_ids(self._run('"uber', refs)), ['id-a'])
        self.assertEqual(self._matched_ids(self._run('über alles', refs)), ['id-a'])
        self.assertEqual(self._matched_ids(self._run('ÜBER ALLES', refs)), ['id-a'])

    def test_whole_word_still_finds_an_escaped_spelling(self) -> None:
        # The prefilter runs on the raw line, where an escaped character puts a
        # backslash at the word's edge - no word boundary at all, where the
        # parsed text plainly has one. Anchoring the prefilter the way the
        # matcher is anchored therefore dropped the line and hid the hit.
        line = json.dumps({'type': 'user', 'message': {'content': 'über alles'}}, ensure_ascii=True) + '\n'
        self._write('id-a', _CWD, line)
        refs = [self._ref('id-a')]

        self.assertEqual(self._matched_ids(self._run('über', refs, {'whole_word': True})), ['id-a'])
        # Whole-word itself must still bite - the anchors live on the matcher.
        self.assertEqual(self._matched_ids(self._run('übe', refs, {'whole_word': True})), [])

    def test_an_ascii_escaped_transcript_is_still_searched(self) -> None:
        # A writer using ASCII-safe JSON spells a non-ASCII character \uXXXX.
        # The prefilter allows for that spelling, so the hit is not lost.
        line = json.dumps({'type': 'user', 'message': {'content': 'Grüße von der Straße'}}, ensure_ascii=True) + '\n'
        self.assertIn('\\u00fc', line)
        self._write('id-a', _CWD, line)

        self.assertEqual(self._matched_ids(self._run('Grüße', [self._ref('id-a')])), ['id-a'])


class SearchBoundaryTest(SearchEnvTest):
    def test_excerpts_are_bounded_in_number(self) -> None:
        # Far more hits than excerpts: the count reports them all, the excerpts
        # stop at the cap, so a match can never grow into a transcript dump.
        self._write('id-a', _CWD, self._entry(' needle' * (search._MAX_SNIPPETS + 12)))

        match = self._matches(self._run('needle', [self._ref('id-a')]))[0]

        self.assertEqual(match['count'], search._MAX_SNIPPETS + 12)
        self.assertEqual(len(match['snippets']), search._MAX_SNIPPETS)

    def test_a_count_that_stopped_short_says_so(self) -> None:
        # Counting is what makes this scan read a whole file, so it gives up past
        # a cap. The result must then be marked partial - reporting the floor as
        # a total would be a number the scan never established.
        entries = ''.join(self._entry('needle') for _ in range(search._MAX_MATCH_ENTRIES + 5))
        self._write('id-a', _CWD, entries)

        match = self._matches(self._run('needle', [self._ref('id-a')]))[0]

        self.assertTrue(match['partial'])
        self.assertEqual(match['count'], search._MAX_MATCH_ENTRIES)

    def test_a_count_inside_the_cap_is_exact(self) -> None:
        entries = ''.join(self._entry('needle') for _ in range(search._MAX_MATCH_ENTRIES - 1))
        self._write('id-a', _CWD, entries)

        match = self._matches(self._run('needle', [self._ref('id-a')]))[0]

        self.assertNotIn('partial', match)
        self.assertEqual(match['count'], search._MAX_MATCH_ENTRIES - 1)

    def test_text_beyond_the_context_window_is_never_reported(self) -> None:
        far = 'SECRET_BODY' + ('x' * search._CONTEXT_CHARS)
        self._write('id-a', _CWD, self._entry(far + ' findme ' + far[::-1]))

        match = self._matches(self._run('findme', [self._ref('id-a')]))[0]

        self.assertEqual(match['session_id'], 'id-a')
        for snippet in match['snippets']:
            reported = snippet['before'] + snippet['match'] + snippet['after']
            self.assertNotIn('SECRET_BODY', reported)
            self.assertLessEqual(len(snippet['before']), search._CONTEXT_CHARS)
            self.assertLessEqual(len(snippet['after']), search._CONTEXT_CHARS)

    def test_a_greedy_regex_cannot_report_the_whole_entry(self) -> None:
        self._write('id-a', _CWD, self._entry('start ' + ('y' * 4000) + ' end'))

        match = self._matches(self._run('.+', [self._ref('id-a')], {'use_regex': True}))[0]

        for snippet in match['snippets']:
            self.assertLessEqual(len(snippet['match']), search._MAX_MATCH_CHARS)

    def test_path_traversal_is_confined_to_projects(self) -> None:
        # A file outside projects/ that a crafted id would resolve to via `..`.
        secret = config_dir() / 'outside-secret.jsonl'
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text('match', encoding='utf-8')

        refs = [self._ref('../../outside-secret')]

        self.assertEqual(self._matched_ids(self._run('match', refs)), [])

class SearchOriginTest(SearchEnvTest):
    """Each ref's root is resolved by its own ``origin``, not assumed to be the Windows root."""

    def _wsl_root_with_transcript(self, base: str, text: str) -> SessionRoot:
        root = SessionRoot(
            origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
            proc_dir=None, claude_temp_dir=Path(base) / 'tmp',
        )
        project = root.config_dir / 'projects' / '-home-dev-proj'
        project.mkdir(parents=True)
        (project / f'{WSL_SID}.jsonl').write_text(
            json.dumps({'type': 'user', 'message': {'content': text}}) + '\n', encoding='utf-8')
        return root

    def test_wsl_ref_matches_via_its_origin(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
            refs = [{'session_id': WSL_SID, 'cwd': '/home/dev/proj', 'origin': 'wsl:U'}]
            with mock.patch.object(search, 'root_for_origin', side_effect=lambda o: root if o == 'wsl:U' else None):
                updates = self._run('needle-in-wsl', refs)

        self.assertIn(WSL_SID, self._matched_ids(updates))

    def test_unknown_origin_scans_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            # The file exists and its content matches - only the unresolved
            # origin must be why nothing is found.
            self._wsl_root_with_transcript(base, 'needle-in-wsl')
            refs = [{'session_id': WSL_SID, 'cwd': '/home/dev/proj', 'origin': 'nope'}]
            with mock.patch.object(search, 'root_for_origin', return_value=None):
                updates = self._run('needle-in-wsl', refs)

        self.assertEqual(self._matched_ids(updates), [])

    def test_wsl_confinement_refuses_escaping_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
            secret = Path(base) / 'outside.jsonl'
            secret.write_text('needle-in-wsl\n', encoding='utf-8')
            # A cwd crafted so slug + id would escape projects/ must be refused per root,
            # mirroring the existing Windows confinement case.
            refs = [{'session_id': WSL_SID, 'cwd': '../../..', 'origin': 'wsl:U'}]
            with mock.patch.object(search, 'root_for_origin', return_value=root):
                updates = self._run('needle-in-wsl', refs)

        self.assertEqual(self._matched_ids(updates), [])

    def test_cross_root_results_stay_ordered_newest_first(self) -> None:
        # An old Windows session and a fresh WSL one, scanned in the same call:
        # the merged, sorted result must still put the newer one first, proving
        # roots are combined into one ordering rather than scanned root-by-root.
        self._write('win-id', _CWD, 'needle-in-wsl', mtime=1000)

        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
            refs = [
                {'session_id': 'win-id', 'cwd': _CWD, 'origin': 'local'},
                {'session_id': WSL_SID, 'cwd': '/home/dev/proj', 'origin': 'wsl:U'},
            ]

            def resolve(origin: object) -> SessionRoot | None:
                return root if origin == 'wsl:U' else _windows_only_root(origin)

            with mock.patch.object(search, 'root_for_origin', side_effect=resolve):
                updates = self._run('needle-in-wsl', refs)

        self.assertEqual(self._matched_ids(updates), [WSL_SID, 'win-id'])


if __name__ == '__main__':
    unittest.main()
