"""Tests for the transcript tail parser (_parse control-flow classification)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript as tmod
from agent_monitor_for_claude.paths import cwd_to_slug, windows_root
from agent_monitor_for_claude.transcript import (
    _absorb_line,
    _parse,
    _run_timeline,
    _scan_title_cwd,
    _ScanState,
    history_state_for,
)


def _lines(*entries: dict) -> list[str]:
    return [json.dumps(entry) for entry in entries]


_CONTINUATION = 'This session is being continued from a previous conversation that ran out of context.'


class InterruptVsToolResultTest(unittest.TestCase):
    def test_interrupt_marker_wins_over_a_tool_result_in_the_same_entry(self) -> None:
        # An interrupt during a tool call could write a single user entry carrying
        # both the marker text and the tool_result. The trailing turn was stopped,
        # so it must read as interrupted, not be silently downgraded to
        # tool_result (which would show green "working" instead of "Interrupted").
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'content': [
                {'type': 'text', 'text': '[Request interrupted by user]'},
                {'type': 'tool_result', 'tool_use_id': 'abc123'},
            ]},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'user_interrupt')

    def test_a_plain_tool_result_still_reads_as_tool_result(self) -> None:
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'abc123'}]},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'tool_result')

    def test_interrupt_entry_still_resolves_its_tool_use(self) -> None:
        # The fix keeps last_entry_kind as user_interrupt but must still record the
        # tool_result's id, so a preceding tool_use is not left pending.
        state = _parse(_lines(
            {
                'type': 'assistant', 'timestamp': '2026-07-11T09:00:00Z',
                'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-4-8',
                            'content': [{'type': 'tool_use', 'id': 'abc123', 'name': 'Bash'}]},
            },
            {
                'type': 'user', 'timestamp': '2026-07-11T09:00:01Z',
                'message': {'content': [
                    {'type': 'text', 'text': '[Request interrupted by user]'},
                    {'type': 'tool_result', 'tool_use_id': 'abc123'},
                ]},
            },
        ))
        self.assertEqual(state.last_entry_kind, 'user_interrupt')
        self.assertFalse(state.pending_tool)


class TitleSkipsInjectedMetaTest(unittest.TestCase):
    def test_absorb_line_ignores_a_meta_user_entry_for_the_first_prompt(self) -> None:
        # An injected isMeta user entry (a continuation summary) must not become
        # the session title - the first real prompt must, mirroring _parse.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user', 'isMeta': True, 'message': {'content': _CONTINUATION},
        }).encode('utf-8'), state)
        self.assertIsNone(state.first_prompt)

        _absorb_line(json.dumps({
            'type': 'user', 'message': {'content': 'the real first prompt'},
        }).encode('utf-8'), state)
        self.assertEqual(state.first_prompt, 'the real first prompt')

    def test_scan_title_cwd_ignores_a_meta_user_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join([
                json.dumps({'type': 'user', 'isMeta': True, 'cwd': 'd:\\proj',
                            'message': {'content': _CONTINUATION}}),
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': 'the real first prompt'}}),
            ]), encoding='utf-8')

            title, cwd = _scan_title_cwd(path)

            self.assertEqual(title, 'the real first prompt')
            self.assertEqual(cwd, 'd:\\proj')


class TitleLooksPastAClearCommandTest(unittest.TestCase):
    def test_absorb_line_keeps_looking_past_a_leading_clear(self) -> None:
        # Every post-/clear session opens with the /clear entry itself, so
        # titling the session after it says nothing about what the session is
        # about - the next real prompt must win.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user',
            'message': {'content': '<command-name>/clear</command-name><command-message>clear</command-message>'},
        }).encode('utf-8'), state)
        self.assertIsNone(state.first_prompt)

        _absorb_line(json.dumps({
            'type': 'user', 'message': {'content': 'the real first prompt'},
        }).encode('utf-8'), state)
        self.assertEqual(state.title(), 'the real first prompt')

    def test_a_meaningful_command_after_clear_becomes_the_title(self) -> None:
        # A session driven by a slash command (/work-on-issue, /pr-review, ...)
        # is best titled after that command - only /clear is housekeeping.
        state = _ScanState()
        for content in ('<command-name>/clear</command-name>', '<command-name>/work-on-issue</command-name>'):
            _absorb_line(json.dumps({
                'type': 'user', 'message': {'content': content},
            }).encode('utf-8'), state)
        self.assertEqual(state.title(), '/work-on-issue')

    def test_a_leading_meaningful_command_keeps_the_title(self) -> None:
        # A meaningful opening command is not displaced by a later prompt -
        # the first-prompt rule is unchanged for everything but /clear.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user', 'message': {'content': '<command-name>/pr-review</command-name>'},
        }).encode('utf-8'), state)
        _absorb_line(json.dumps({
            'type': 'user', 'message': {'content': 'here is more detail'},
        }).encode('utf-8'), state)
        self.assertEqual(state.title(), '/pr-review')

    def test_a_command_title_carries_its_arguments(self) -> None:
        # "/work-on-issue #123" names the session; the bare command name only
        # says which workflow ran, not on what.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user',
            'message': {'content': '<command-name>/work-on-issue</command-name><command-args>#123</command-args>'},
        }).encode('utf-8'), state)
        self.assertEqual(state.title(), '/work-on-issue #123')

    def test_multi_line_command_arguments_are_collapsed(self) -> None:
        # <command-args> matches across newlines (re.S); raw newlines must never
        # reach a title - it lands in data-tip attributes, where a newline
        # renders as a line break. Collapsed exactly like every other title.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user',
            'message': {'content': '<command-name>/work-on-issue</command-name>'
                                   '<command-args>#123\nextra   context</command-args>'},
        }).encode('utf-8'), state)
        self.assertEqual(state.title(), '/work-on-issue #123 extra context')

    def test_long_command_arguments_are_clipped_like_any_title(self) -> None:
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user',
            'message': {'content': '<command-name>/work-on-issue</command-name><command-args>'
                                   + 'x' * 200 + '</command-args>'},
        }).encode('utf-8'), state)
        title = state.title()
        self.assertTrue(title.startswith('/work-on-issue x'))
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith('…'))

    def test_a_clear_only_session_still_falls_back_to_the_command_name(self) -> None:
        # With nothing after /clear anywhere, its name is still better than an
        # id-derived name - the old behaviour remains the last resort.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'user',
            'message': {'content': '<command-name>/clear</command-name>'},
        }).encode('utf-8'), state)
        self.assertEqual(state.title(), '/clear')

    def test_scan_title_cwd_skips_a_leading_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join([
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': '<command-name>/clear</command-name>'}}),
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': 'the real first prompt'}}),
            ]), encoding='utf-8')

            title, cwd = _scan_title_cwd(path)

            self.assertEqual(title, 'the real first prompt')
            self.assertEqual(cwd, 'd:\\proj')


_LIMIT_ERROR = {
    'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'isApiErrorMessage': True,
    'apiErrorStatus': 429, 'message': {'stop_reason': 'error', 'model': 'claude-opus-4-8', 'usage': {}},
}


class UsageLimitedResetTest(unittest.TestCase):
    def test_trailing_usage_limit_sets_the_flag(self) -> None:
        state = _parse(_lines(_LIMIT_ERROR))
        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertTrue(state.usage_limited)

    def test_usage_limited_is_reset_when_a_later_turn_supersedes_the_error(self) -> None:
        # The CLI retried past a mid-conversation 429: last_entry_kind moves on, so
        # the usage_limited flag must not linger True for the rest of the transcript.
        state = _parse(_lines(
            _LIMIT_ERROR,
            {'type': 'assistant', 'timestamp': '2026-07-11T10:01:00Z',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {}}},
        ))
        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertFalse(state.usage_limited)

    def test_usage_limited_is_reset_by_a_later_user_turn(self) -> None:
        state = _parse(_lines(
            _LIMIT_ERROR,
            {'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'message': {'content': 'try again'}},
        ))
        self.assertEqual(state.last_entry_kind, 'user_text')
        self.assertFalse(state.usage_limited)


class ModelEventGuardTest(unittest.TestCase):
    def test_non_string_model_is_not_recorded_as_an_event(self) -> None:
        # A mistyped, truthy non-string model must not reach model_timeline, which
        # crosses the bridge and would make formatModel operate on a non-string.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
            'message': {'stop_reason': 'end_turn', 'model': 123, 'usage': {'input_tokens': 5}},
        }).encode('utf-8'), state)
        self.assertEqual(state.model_events, [])

    def test_string_model_is_recorded(self) -> None:
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
            'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {'input_tokens': 5}},
        }).encode('utf-8'), state)
        self.assertEqual(state.model_events, [('2026-07-11T10:00:00Z', 'claude-opus-4-8')])


class ModelTimelineOrderTest(unittest.TestCase):
    def test_sorts_chronologically_not_lexicographically(self) -> None:
        # '...07.500Z' is chronologically LATER than '...07Z' but sorts BEFORE it
        # as a raw string ('.' < 'Z'), so a lexicographic sort would name the
        # wrong model as current.
        timeline = _run_timeline([
            ('2026-07-11T10:53:07Z', 'opus'),
            ('2026-07-11T10:53:07.500Z', 'sonnet'),
        ], 'model')
        self.assertEqual([entry['model'] for entry in timeline], ['opus', 'sonnet'])
        self.assertEqual(timeline[-1]['time'], '2026-07-11T10:53:07.500Z')

    def test_unparseable_timestamps_do_not_crash(self) -> None:
        timeline = _run_timeline([('not-a-timestamp', 'opus'), ('2026-07-11T10:00:00Z', 'sonnet')], 'model')
        self.assertEqual({entry['model'] for entry in timeline}, {'opus', 'sonnet'})


class CliVersionTest(unittest.TestCase):
    """The Claude Code version each turn was written by."""

    def test_tail_reports_the_newest_version_of_any_entry_kind(self) -> None:
        # The version is stamped on every entry, not just assistant turns, so a
        # session whose newest entry is a user prompt still reports a version.
        state = _parse(_lines(
            {'type': 'assistant', 'timestamp': '2026-07-11T09:00:00Z', 'version': '2.1.224',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {}}},
            {'type': 'user', 'timestamp': '2026-07-11T09:05:00Z', 'version': '2.1.228',
             'message': {'content': 'carry on'}},
        ))
        self.assertEqual(state.cli_version, '2.1.228')

    def test_missing_or_mistyped_version_leaves_it_unset(self) -> None:
        state = _parse(_lines(
            {'type': 'assistant', 'timestamp': '2026-07-11T09:00:00Z', 'version': 21224,
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {}}},
            {'type': 'user', 'timestamp': '2026-07-11T09:05:00Z', 'message': {'content': 'carry on'}},
        ))
        self.assertIsNone(state.cli_version)

    def test_events_are_recorded_for_main_conversation_turns_only(self) -> None:
        state = _ScanState()
        for entry in (
            {'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'version': '2.1.224',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {'input_tokens': 5}}},
            # A subagent's turn runs in the same CLI, so it adds nothing - and must
            # not date a switch the main conversation never made.
            {'type': 'assistant', 'timestamp': '2026-07-11T10:01:00Z', 'version': '2.1.999', 'isSidechain': True,
             'message': {'stop_reason': 'end_turn', 'model': 'claude-haiku-4-5', 'usage': {'input_tokens': 5}}},
            # A mistyped version must never reach the timeline, which crosses the
            # bridge and is matched against a release-number pattern there.
            {'type': 'assistant', 'timestamp': '2026-07-11T10:02:00Z', 'version': 21226,
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {'input_tokens': 5}}},
        ):
            _absorb_line(json.dumps(entry).encode('utf-8'), state)

        self.assertEqual(state.cli_events, [('2026-07-11T10:00:00Z', '2.1.224')])

    def test_synthetic_model_turn_still_reports_its_version(self) -> None:
        # The synthetic sentinel means "no real model", not "no real CLI": the
        # turn was still written by a version, so it is valid evidence even
        # though it is excluded from the model timeline.
        state = _ScanState()
        _absorb_line(json.dumps({
            'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'version': '2.1.228',
            'message': {'stop_reason': 'end_turn', 'model': '<synthetic>', 'usage': {}},
        }).encode('utf-8'), state)
        self.assertEqual(state.model_events, [])
        self.assertEqual(state.cli_events, [('2026-07-11T10:00:00Z', '2.1.228')])

    def test_timeline_compresses_runs_and_keeps_a_returned_version(self) -> None:
        # Same run-length compression as the model timeline: consecutive equal
        # versions collapse, and a downgrade back to an earlier version appears
        # again rather than being folded into its first run.
        timeline = _run_timeline([
            ('2026-07-11T09:00:00Z', '2.1.224'),
            ('2026-07-11T09:30:00Z', '2.1.224'),
            ('2026-07-11T11:00:00Z', '2.1.228'),
            ('2026-07-11T13:00:00Z', '2.1.224'),
        ], 'version')
        self.assertEqual(timeline, [
            {'time': '2026-07-11T09:00:00Z', 'version': '2.1.224'},
            {'time': '2026-07-11T11:00:00Z', 'version': '2.1.228'},
            {'time': '2026-07-11T13:00:00Z', 'version': '2.1.224'},
        ])

    def test_timeline_sorts_chronologically(self) -> None:
        # Transcript entries are not strictly ordered on disk, and a fractional
        # second sorts before a whole one as a raw string though it is later.
        timeline = _run_timeline([
            ('2026-07-11T10:53:07.500Z', '2.1.228'),
            ('2026-07-11T10:53:07Z', '2.1.224'),
        ], 'version')
        self.assertEqual([entry['version'] for entry in timeline], ['2.1.224', '2.1.228'])

    def test_history_state_reports_the_version_from_the_tail(self) -> None:
        entries = [
            {'type': 'assistant', 'timestamp': '2026-07-11T09:00:00Z', 'version': '2.1.207',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-haiku-4-5', 'usage': {'input_tokens': 5}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')
            state = history_state_for(path)

        self.assertEqual(state.cli_version, '2.1.207')


class HistoryStateEscalationTest(unittest.TestCase):
    def test_escalates_the_tail_window_for_a_giant_final_entry(self) -> None:
        # A history transcript ending in a single entry larger than the 256 KB
        # default window must still recover the model and the last-turn timestamp
        # (not fall back to file mtime), just like the live path does.
        big_text = 'x' * 300000  # push the single entry past the 256 KB window
        entry = {
            'type': 'assistant', 'timestamp': '2020-01-01T00:00:00Z',
            'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                        'usage': {'input_tokens': 5}, 'content': [{'type': 'text', 'text': big_text}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(json.dumps(entry) + '\n', encoding='utf-8')
            state = history_state_for(path)

        self.assertEqual(state.model, 'claude-opus-4-8')
        self.assertIsNotNone(state.age_seconds)
        # Age from the 2020 timestamp (years), not the just-written file mtime (~0).
        self.assertGreater(state.age_seconds, 365 * 24 * 3600)


class TailEscalationTest(unittest.TestCase):
    def test_parse_marks_a_parseable_sidechain_tail(self) -> None:
        state = _parse(_lines({
            'type': 'assistant', 'isSidechain': True, 'timestamp': '2026-07-11T10:00:00Z',
            'message': {'model': 'x', 'usage': {}},
        }))
        self.assertTrue(state.any_parsed)
        # A sidechain turn is skipped for the main conversation's timestamp/kind.
        self.assertIsNone(state.last_timestamp)
        self.assertIsNone(state.last_entry_kind)

    def test_parse_marks_an_unparseable_tail(self) -> None:
        self.assertFalse(_parse(['{ not json']).any_parsed)
        self.assertFalse(_parse([]).any_parsed)

    def test_state_for_stops_escalating_once_a_line_parses(self) -> None:
        # A parseable-but-timestampless tail (sidechain only) must not escalate to
        # the larger windows - that would re-read up to 16 MB on every poll.
        previous = os.environ.get('CLAUDE_CONFIG_DIR')
        temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = temp.name
        tmod._scan_cache.clear()
        try:
            session_id, cwd = 'aaaaaaaa', 'd:\\proj'
            slug_dir = Path(temp.name) / 'projects' / cwd_to_slug(cwd)
            slug_dir.mkdir(parents=True)
            (slug_dir / f'{session_id}.jsonl').write_text(
                json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'model': 'x', 'usage': {}}}) + '\n',
                encoding='utf-8',
            )

            calls = []
            real_read_tail = tmod._read_tail

            def spy(path, *args):
                calls.append(1)
                return real_read_tail(path, *args)

            with mock.patch.object(tmod, '_read_tail', side_effect=spy):
                tmod.state_for(windows_root(), session_id, cwd)

            self.assertEqual(len(calls), 1, 'a parseable tail must not escalate the read window')
        finally:
            tmod._scan_cache.clear()
            if previous is None:
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
            else:
                os.environ['CLAUDE_CONFIG_DIR'] = previous
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
