"""Tests for the transcript tail parser (_parse control-flow classification)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript as tmod
from agent_monitor_for_claude.paths import cwd_to_slug, local_root
from agent_monitor_for_claude.transcript import (
    _absorb_line,
    _parse,
    _run_timeline,
    _scan_history,
    _ScanState,
    _timestamp_epoch,
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


class LocalCommandOutputTest(unittest.TestCase):
    """A local command's captured output is its execution record, not a prompt.

    Newer Claude Code versions end a `/compact` (and other local commands) with
    an ordinary ``user`` entry wrapping the command's output instead of the
    ``system``/``local_command`` record.  Read as a fresh prompt it would leave
    the session permanently "working", because nothing is ever appended after it.
    """

    def test_captured_stdout_reads_as_a_local_command(self) -> None:
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'role': 'user', 'content': '<local-command-stdout>Compacted </local-command-stdout>'},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'local_command')

    def test_captured_stderr_reads_as_a_local_command(self) -> None:
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'role': 'user', 'content': '<local-command-stderr>boom</local-command-stderr>'},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'local_command')

    def test_a_block_list_carries_the_marker_too(self) -> None:
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'content': [{'type': 'text', 'text': '<local-command-stdout>x</local-command-stdout>'}]},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'local_command')

    def test_a_command_invocation_still_reads_as_a_prompt(self) -> None:
        # A slash command that briefs the model (`/loop ...`) is recorded as a
        # plain user entry naming the command. The model owes a reply to it, so
        # it must stay user_text ("working") - only the *output* wrapper above
        # means the command already ran outside the model.
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'role': 'user',
                        'content': '<command-message>loop</command-message>\n<command-name>/loop</command-name>'},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'user_text')

    def test_the_interrupt_marker_still_wins(self) -> None:
        entry = {
            'type': 'user',
            'timestamp': '2026-07-11T09:00:00Z',
            'message': {'content': [
                {'type': 'text', 'text': '[Request interrupted by user]'},
                {'type': 'text', 'text': '<local-command-stdout>x</local-command-stdout>'},
            ]},
        }
        state = _parse(_lines(entry))
        self.assertEqual(state.last_entry_kind, 'user_interrupt')

    def test_trailing_attachments_do_not_reopen_the_turn(self) -> None:
        # What a real `/compact` tail looks like: the caveat (isMeta), the command
        # entry, its captured output, then only attachment/title records - none of
        # which is a turn. The newest kind must stay local_command.
        state = _parse(_lines(
            {'type': 'user', 'isMeta': True, 'timestamp': '2026-07-11T09:00:00Z',
             'message': {'role': 'user', 'content': '<local-command-caveat>x</local-command-caveat>'}},
            {'type': 'user', 'timestamp': '2026-07-11T09:00:01Z',
             'message': {'role': 'user', 'content': '<command-name>/compact</command-name>'}},
            {'type': 'user', 'timestamp': '2026-07-11T09:00:02Z',
             'message': {'role': 'user', 'content': '<local-command-stdout>Compacted </local-command-stdout>'}},
            {'type': 'attachment', 'timestamp': '2026-07-11T09:00:03Z'},
            {'type': 'last-prompt'},
            {'type': 'ai-title'},
        ))
        self.assertEqual(state.last_entry_kind, 'local_command')

    def test_a_later_prompt_supersedes_the_command_record(self) -> None:
        state = _parse(_lines(
            {'type': 'user', 'timestamp': '2026-07-11T09:00:00Z',
             'message': {'role': 'user', 'content': '<local-command-stdout>Compacted </local-command-stdout>'}},
            {'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'role': 'user', 'content': 'carry on'}},
        ))
        self.assertEqual(state.last_entry_kind, 'user_text')

    def test_a_mistyped_or_missing_text_field_degrades_to_a_prompt(self) -> None:
        # Defensive parsing: the marker check must never crash on a text block
        # whose `text` is absent or not a string, and an entry it cannot read
        # stays the ordinary user turn rather than being claimed as a command.
        for block in ({'type': 'text', 'text': 123}, {'type': 'text'}, {'type': 'image'}, 'not-a-dict'):
            with self.subTest(block=block):
                entry = {'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': [block]}}
                state = _parse(_lines(entry))
                self.assertEqual(state.last_entry_kind, 'user_text')


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

    def test_scan_history_ignores_a_meta_user_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join([
                json.dumps({'type': 'user', 'isMeta': True, 'cwd': 'd:\\proj',
                            'message': {'content': _CONTINUATION}}),
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': 'the real first prompt'}}),
            ]), encoding='utf-8')

            state, cwd = _scan_history(path)
            title = state.title()

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

    def test_scan_history_skips_a_leading_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text('\n'.join([
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': '<command-name>/clear</command-name>'}}),
                json.dumps({'type': 'user', 'cwd': 'd:\\proj',
                            'message': {'content': 'the real first prompt'}}),
            ]), encoding='utf-8')

            state, cwd = _scan_history(path)
            title = state.title()

            self.assertEqual(title, 'the real first prompt')
            self.assertEqual(cwd, 'd:\\proj')


_LIMIT_ERROR = {
    'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'isApiErrorMessage': True,
    'apiErrorStatus': 429, 'error': 'rate_limit',
    'message': {'stop_reason': 'error', 'model': 'claude-opus-4-8', 'usage': {}},
}


class ApiErrorFieldsTest(unittest.TestCase):
    def test_trailing_error_reports_its_own_fields(self) -> None:
        state = _parse(_lines(_LIMIT_ERROR))
        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertEqual(state.api_error_status, 429)
        self.assertEqual(state.api_error_kind, 'rate_limit')

    def test_a_status_written_as_a_string_is_still_read(self) -> None:
        # Defensive: the field is an unversioned internal; a string reading must
        # not degrade the status to None and cost the label its cause.
        state = _parse(_lines({**_LIMIT_ERROR, 'apiErrorStatus': '529', 'error': 'server_error'}))
        self.assertEqual(state.api_error_status, 529)
        self.assertEqual(state.api_error_kind, 'server_error')

    def test_an_unusable_status_degrades_to_none_but_keeps_the_token(self) -> None:
        # A failure that never reached the API carries no status at all, and a
        # mistyped one must reach the UI as nothing but None.
        state = _parse(_lines({**_LIMIT_ERROR, 'apiErrorStatus': 'n/a', 'error': 'server_error'}))
        self.assertIsNone(state.api_error_status)
        self.assertEqual(state.api_error_kind, 'server_error')

    def test_the_error_message_first_line_is_read_for_the_tooltip(self) -> None:
        # The wording carries what the two structural fields cannot - here the
        # reset time - so the first line is read, and only that: a second line
        # and any later block must stay unread.
        state = _parse(_lines({
            **_LIMIT_ERROR,
            'message': {
                'stop_reason': 'stop_sequence', 'model': '<synthetic>', 'usage': {},
                'content': [
                    {'type': 'text', 'text': "You've hit your session limit - resets 11:30pm\nUNREAD_SECOND_LINE"},
                    {'type': 'text', 'text': 'UNREAD_SECOND_BLOCK'},
                ],
            },
        }))
        self.assertEqual(state.api_error_detail, "You've hit your session limit - resets 11:30pm")

    def test_a_long_error_message_line_is_clipped(self) -> None:
        state = _parse(_lines({
            **_LIMIT_ERROR,
            'message': {'stop_reason': 'stop_sequence', 'usage': {}, 'content': [{'type': 'text', 'text': 'x' * 400}]},
        }))
        self.assertEqual(len(state.api_error_detail), 160)
        self.assertTrue(state.api_error_detail.endswith('…'))

    def test_an_error_without_readable_text_reports_no_detail(self) -> None:
        # Nothing to show is None, never an empty string the UI would render as
        # a blank second tooltip line.
        for content in ([], [{'type': 'thinking', 'thinking': 'unread'}], [{'type': 'text', 'text': '   \n  '}], None, 42):
            with self.subTest(content=content):
                state = _parse(_lines({
                    **_LIMIT_ERROR,
                    'message': {'stop_reason': 'stop_sequence', 'usage': {}, 'content': content},
                }))
                self.assertEqual(state.last_entry_kind, 'api_error')
                self.assertIsNone(state.api_error_detail)

    def test_error_fields_are_reset_when_a_later_turn_supersedes_the_error(self) -> None:
        # The CLI retried past a mid-conversation 429: last_entry_kind moves on, so
        # the error fields must not linger for the rest of the transcript.
        state = _parse(_lines(
            _LIMIT_ERROR,
            {'type': 'assistant', 'timestamp': '2026-07-11T10:01:00Z',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8', 'usage': {}}},
        ))
        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertIsNone(state.api_error_status)
        self.assertIsNone(state.api_error_kind)
        self.assertIsNone(state.api_error_detail)

    def test_error_fields_are_reset_by_a_later_user_turn(self) -> None:
        state = _parse(_lines(
            _LIMIT_ERROR,
            {'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'message': {'content': 'try again'}},
        ))
        self.assertEqual(state.last_entry_kind, 'user_text')
        self.assertIsNone(state.api_error_status)
        self.assertIsNone(state.api_error_kind)
        self.assertIsNone(state.api_error_detail)

    def test_a_usage_limit_names_its_limit_and_reset(self) -> None:
        state = _parse(_lines({**_LIMIT_ERROR, 'quotaLimits': {'status': 'rejected', 'resetsAt': 1793613600, 'rateLimitType': 'seven_day'}}))
        self.assertEqual(state.rate_limit_type, 'seven_day')
        self.assertEqual(state.rate_limit_resets_at, 1793613600.0)

    def test_a_mistyped_reset_degrades_on_its_own(self) -> None:
        # The limit still names itself when its reset does not read as a number,
        # and an error without quotaLimits carries neither.
        for limits in ({'resetsAt': 'soon', 'rateLimitType': 'five_hour'}, {'resetsAt': True, 'rateLimitType': 'five_hour'}):
            with self.subTest(limits=limits):
                state = _parse(_lines({**_LIMIT_ERROR, 'quotaLimits': limits}))
                self.assertEqual(state.rate_limit_type, 'five_hour')
                self.assertIsNone(state.rate_limit_resets_at)
        state = _parse(_lines(_LIMIT_ERROR))
        self.assertIsNone(state.rate_limit_type)
        self.assertIsNone(state.rate_limit_resets_at)

    def test_the_reset_is_cleared_by_a_later_turn(self) -> None:
        state = _parse(_lines(
            {**_LIMIT_ERROR, 'quotaLimits': {'resetsAt': 1793613600, 'rateLimitType': 'seven_day'}},
            {'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'message': {'content': 'continue'}},
        ))
        self.assertIsNone(state.rate_limit_type)
        self.assertIsNone(state.rate_limit_resets_at)


_PROMPT = {'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'message': {'content': 'do it'}}


def _retry(attempt: int = 2, **error: object) -> dict:
    return {'type': 'system', 'subtype': 'api_error', 'timestamp': '2026-07-11T10:00:05Z', 'level': 'error',
            'retryAttempt': attempt, 'maxRetries': 10, 'retryInMs': 1098, 'error': error}


def _tool_call(name: str, timestamp: str = '2026-07-11T10:00:10Z', tool_id: str = 't1', **tool_input: object) -> dict:
    return {'type': 'assistant', 'timestamp': timestamp,
            'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-5-5',
                        'content': [{'type': 'tool_use', 'id': tool_id, 'name': name, 'input': tool_input}]}}


def _tool_result(tool_id: str = 't1', timestamp: str = '2026-07-11T10:00:11Z') -> dict:
    return {'type': 'user', 'timestamp': timestamp, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_id}]}}


_END_TURN = {'type': 'assistant', 'timestamp': '2026-07-11T10:00:12Z',
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'done'}]}}


class ApiRetryTest(unittest.TestCase):
    def test_a_retried_request_is_reported_without_changing_the_kind(self) -> None:
        # The turn is still running, so the kind stays the prompt's; the retry
        # only refines what the session is doing meanwhile.
        state = _parse(_lines(_PROMPT, _retry(2, formatted='Connection dropped (ECONNRESET)', isNetworkDown=False)))
        self.assertEqual(state.last_entry_kind, 'user_text')
        self.assertEqual(state.api_retry, tmod.ApiRetry(attempt=2, max_attempts=10, offline=False, detail='Connection dropped (ECONNRESET)'))

    def test_claude_code_own_offline_verdict_is_kept(self) -> None:
        state = _parse(_lines(_PROMPT, _retry(3, isNetworkDown=True)))
        self.assertTrue(state.api_retry.offline)
        self.assertIsNone(state.api_retry.detail)

    def test_the_retry_is_cleared_once_the_request_goes_through_or_gives_up(self) -> None:
        for later in (_END_TURN, _LIMIT_ERROR, _PROMPT):
            with self.subTest(later=later['type']):
                self.assertIsNone(_parse(_lines(_PROMPT, _retry(), later)).api_retry)

    def test_unreadable_counters_record_no_retry(self) -> None:
        for attempt in (None, 0, '2', True):
            with self.subTest(attempt=attempt):
                self.assertIsNone(_parse(_lines(_PROMPT, _retry(attempt))).api_retry)

    def test_a_subagent_retry_is_not_the_main_conversation(self) -> None:
        self.assertIsNone(_parse(_lines(_PROMPT, {**_retry(), 'isSidechain': True})).api_retry)


class ScheduledWakeupTest(unittest.TestCase):
    _SCHEDULED = datetime(2026, 7, 11, 10, 0, 10, tzinfo=timezone.utc).timestamp()

    def test_a_wakeup_is_due_at_its_call_time_plus_delay(self) -> None:
        state = _parse(_lines(_PROMPT, _tool_call('ScheduleWakeup', delaySeconds=270, reason='r', prompt='p'), _tool_result(), _END_TURN))
        self.assertEqual(state.wakeup_at, self._SCHEDULED + 270)

    def test_a_later_call_replaces_it_and_a_stop_ends_it(self) -> None:
        state = _parse(_lines(
            _tool_call('ScheduleWakeup', delaySeconds=270), _tool_result(),
            _tool_call('ScheduleWakeup', tool_id='t2', delaySeconds=60), _tool_result('t2'),
        ))
        self.assertEqual(state.wakeup_at, self._SCHEDULED + 60)
        state = _parse(_lines(
            _tool_call('ScheduleWakeup', delaySeconds=270), _tool_result(),
            _tool_call('ScheduleWakeup', tool_id='t2', stop=True), _tool_result('t2'),
        ))
        self.assertIsNone(state.wakeup_at)

    def test_an_unreadable_delay_schedules_nothing(self) -> None:
        for delay in (None, 'soon', True, -5, 0):
            with self.subTest(delay=delay):
                self.assertIsNone(_parse(_lines(_tool_call('ScheduleWakeup', delaySeconds=delay))).wakeup_at)

    def test_another_tool_leaves_the_wakeup_standing(self) -> None:
        # The loop is still scheduled while the turn goes on with other calls.
        state = _parse(_lines(_tool_call('ScheduleWakeup', delaySeconds=270), _tool_result(), _tool_call('Bash', tool_id='t2')))
        self.assertEqual(state.wakeup_at, self._SCHEDULED + 270)


class TurnEndSignalTest(unittest.TestCase):
    _WRAP_UP = 'mcp__ccd_turn__wrap_up'

    def test_a_blocked_wrap_up_ends_the_turn_blocked(self) -> None:
        state = _parse(_lines(_PROMPT, _tool_call(self._WRAP_UP, recap='r', ask='a?', blocked=True), _tool_result(), _END_TURN))
        self.assertEqual(state.turn_end_signal, 'blocked')

    def test_a_wrap_up_with_a_question_ends_on_it(self) -> None:
        state = _parse(_lines(_PROMPT, _tool_call(self._WRAP_UP, recap='r', ask='a?'), _tool_result(), _END_TURN))
        self.assertEqual(state.turn_end_signal, 'ask')

    def test_a_plain_wrap_up_or_an_empty_question_signals_nothing(self) -> None:
        for tool_input in ({'recap': 'r'}, {'recap': 'r', 'ask': '   '}, {'recap': 'r', 'blocked': 'yes'}):
            with self.subTest(tool_input=tool_input):
                self.assertIsNone(_parse(_lines(_tool_call(self._WRAP_UP, **tool_input))).turn_end_signal)

    def test_the_signal_is_cleared_by_a_later_call_or_prompt(self) -> None:
        blocked = _tool_call(self._WRAP_UP, recap='r', ask='a?', blocked=True)
        self.assertIsNone(_parse(_lines(blocked, _tool_result(), _tool_call('Bash', tool_id='t2'))).turn_end_signal)
        self.assertIsNone(_parse(_lines(blocked, _tool_result(), _END_TURN, _PROMPT, _END_TURN)).turn_end_signal)
        # A loop's wakeup is an injected meta prompt, and it opens a turn too.
        wakeup = {**_PROMPT, 'isMeta': True}
        self.assertIsNone(_parse(_lines(blocked, _tool_result(), _END_TURN, wakeup, _END_TURN)).turn_end_signal)


class TurnOriginTest(unittest.TestCase):
    def test_the_origin_kind_of_the_opening_prompt_is_read(self) -> None:
        for origin, expected in (('human', 'human'), ('task-notification', 'task-notification'), ('peer', 'peer')):
            with self.subTest(origin=origin):
                state = _parse(_lines({**_PROMPT, 'origin': {'kind': origin}}, _tool_call('Bash')))
                self.assertEqual(state.turn_origin, expected)

    def test_the_older_turn_origin_spelling_is_read_too(self) -> None:
        self.assertEqual(_parse(_lines({**_PROMPT, 'turnOrigin': 'task_notification'})).turn_origin, 'task-notification')

    def test_a_peer_message_injected_as_meta_still_opens_the_turn(self) -> None:
        peer = {**_PROMPT, 'isMeta': True, 'origin': {'kind': 'peer', 'from': 'x'}}
        state = _parse(_lines({**_PROMPT, 'origin': {'kind': 'human'}}, _END_TURN, peer, _tool_call('Bash')))
        self.assertEqual(state.turn_origin, 'peer')

    def test_a_tool_result_does_not_open_a_turn(self) -> None:
        state = _parse(_lines({**_PROMPT, 'origin': {'kind': 'task-notification'}}, _tool_call('Bash'), _tool_result()))
        self.assertEqual(state.turn_origin, 'task-notification')

    def test_an_unknown_or_missing_origin_reads_as_none(self) -> None:
        for entry in (_PROMPT, {**_PROMPT, 'origin': {'kind': 'constructor'}}, {**_PROMPT, 'origin': 'human'}, {**_PROMPT, 'turnOrigin': 7}):
            with self.subTest(entry=entry):
                self.assertIsNone(_parse(_lines(entry)).turn_origin)


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


class SchemaDecodeTest(unittest.TestCase):
    """The full scan decodes only the fields it reads; a line of another shape is parsed in full instead."""

    @staticmethod
    def _absorb(state, entry):
        _absorb_line(json.dumps(entry).encode('utf-8'), state)

    def test_a_line_of_an_unexpected_shape_is_still_read(self) -> None:
        # A string where the schema expects an object fails the partial decode;
        # the full parse still reads every other field on the line.
        state = _ScanState()
        self._absorb(state, {'type': 'ai-title', 'aiTitle': 'Renamed', 'message': 'not an object'})
        self._absorb(state, {'type': 'user', 'permissionMode': 'plan', 'toolUseResult': 42, 'message': {'content': 7}})
        self.assertEqual(state.ai_title, 'Renamed')
        self.assertEqual(state.permission_mode, 'plan')

    def test_malformed_json_is_skipped(self) -> None:
        state = _ScanState()
        _absorb_line(b'{"type": "ai-title", "aiTitle": "Half', state)
        _absorb_line(b'', state)
        _absorb_line(b'[1, 2]', state)
        self.assertIsNone(state.ai_title)

    def test_a_failed_tool_result_is_told_from_a_refusal_by_its_content(self) -> None:
        # A tool result's content is left undecoded by the partial decode and
        # read only for an error result - in either of its shapes.
        state = _ScanState()
        for tool_id, content in (
            ('t1', 'Exit code 1'),
            ('t2', [{'type': 'text', 'text': "The user doesn't want to proceed with this tool use."}]),
        ):
            self._absorb(state, {
                'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                'message': {'id': f'msg-{tool_id}', 'content': [{'type': 'tool_use', 'id': tool_id, 'name': 'Bash', 'input': {'command': 'x'}}]},
            })
            self._absorb(state, {
                'type': 'user', 'timestamp': '2026-07-11T10:00:01Z',
                'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True, 'content': content}]},
            })
        self.assertEqual(state.tool_uses['Bash'][0], 2)
        self.assertEqual(state.tool_uses['Bash'][2], 1)

    def test_a_created_file_counts_its_lines_from_the_undecoded_content(self) -> None:
        state = _ScanState()
        self._absorb(state, {
            'type': 'user', 'timestamp': '2026-07-11T10:00:01Z',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]},
            'toolUseResult': {'type': 'create', 'structuredPatch': [], 'content': 'one\ntwo\nthree'},
        })
        self.assertEqual(state.lines_added, 3)

    def test_an_edited_file_is_read_from_the_tool_input_without_its_text(self) -> None:
        state = _ScanState()
        self._absorb(state, {
            'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
            'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Write', 'input': {'file_path': 'a.py', 'content': 'x' * 10000}}]},
        })
        self.assertEqual(state.edited_files, {'a.py'})


class AutoModeDenialStreakTest(unittest.TestCase):
    """The classifier-block streak behind the paused-auto-mode badge."""

    @staticmethod
    def _absorb(state, entry):
        _absorb_line(json.dumps(entry).encode('utf-8'), state)

    @staticmethod
    def _denial(kind):
        return {
            'type': 'user',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'abc', 'is_error': True}]},
            'toolDenialKind': kind,
        }

    @staticmethod
    def _tool_result():
        return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'abc'}]}}

    def test_consecutive_blocks_are_counted(self) -> None:
        state = _ScanState()
        for _ in range(3):
            self._absorb(state, self._denial('automode-blocked'))
        self.assertEqual(state.auto_denials_consecutive, 3)
        self.assertEqual(state.auto_denials_total, 3)

    def test_an_allowed_tool_result_ends_the_streak_but_not_the_total(self) -> None:
        state = _ScanState()
        self._absorb(state, self._denial('automode-blocked'))
        self._absorb(state, self._denial('automode-blocked'))
        self._absorb(state, self._tool_result())
        self._absorb(state, self._denial('automode-blocked'))
        self.assertEqual(state.auto_denials_consecutive, 1)
        self.assertEqual(state.auto_denials_total, 3)

    def test_other_denial_kinds_neither_count_nor_reset(self) -> None:
        # Only a classifier verdict counts towards the pause: a rejection by the
        # user or a deny rule is not one, and a denial raised when a safety check
        # refuses the classifier's own request is documented as not counting.
        # None of them is an *allowed* action either, so none clears the streak.
        state = _ScanState()
        self._absorb(state, self._denial('automode-blocked'))
        for kind in ('user-rejected', 'permission-rule', 'automode-unavailable', 'cancelled'):
            self._absorb(state, self._denial(kind))
        self._absorb(state, self._denial('automode-blocked'))
        self.assertEqual(state.auto_denials_consecutive, 2)
        self.assertEqual(state.auto_denials_total, 2)

    def test_a_subagents_denial_is_not_the_main_conversations_streak(self) -> None:
        state = _ScanState()
        denial = self._denial('automode-blocked')
        denial['isSidechain'] = True
        self._absorb(state, denial)
        self.assertEqual(state.auto_denials_consecutive, 0)
        self.assertEqual(state.auto_denials_total, 0)

    def test_an_ordinary_turn_leaves_the_streak_standing(self) -> None:
        # Only a tool result says a tool ran. An assistant turn between two
        # blocked calls is the model reacting to the block, not an allowed action.
        state = _ScanState()
        self._absorb(state, self._denial('automode-blocked'))
        self._absorb(state, {
            'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
            'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-4-8', 'usage': {'input_tokens': 5}},
        })
        self.assertEqual(state.auto_denials_consecutive, 1)

    def test_the_counts_survive_a_partial_trailing_line(self) -> None:
        # _scan_appended copies the state to absorb a half-written line; the copy
        # is positional, so a field left out of it silently reports zero.
        state = _ScanState()
        self._absorb(state, self._denial('automode-blocked'))
        self.assertEqual(state.copy().auto_denials_consecutive, 1)
        self.assertEqual(state.copy().auto_denials_total, 1)

    def test_a_scan_reports_the_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(
                ''.join(json.dumps(entry) + '\n' for entry in (
                    self._denial('automode-blocked'),
                    self._denial('automode-blocked'),
                )),
                encoding='utf-8',
            )
            tmod._scan_cache.clear()
            try:
                result = tmod._scan_appended(path)
            finally:
                tmod._scan_cache.clear()
            self.assertEqual(result.auto_denials_consecutive, 2)
            self.assertEqual(result.auto_denials_total, 2)


class WorktreeStateTest(unittest.TestCase):
    """The worktree a session switched into, read from Claude Code's worktree-state entries."""

    @staticmethod
    def _absorb(state, entry):
        _absorb_line(json.dumps(entry).encode('utf-8'), state)

    @staticmethod
    def _entered(name, **extra):
        session = {
            'originalCwd': 'D:\\WebDev\\app', 'preEnterOriginalCwd': 'd:\\WebDev\\app',
            'worktreeName': name, 'worktreePath': f'D:\\WebDev\\app-worktrees\\{name}',
        }
        session.update(extra)
        return {'type': 'worktree-state', 'sessionId': 'x', 'worktreeSession': session}

    def test_the_newest_entry_wins(self) -> None:
        state = _ScanState()
        self._absorb(state, self._entered('first'))
        self._absorb(state, self._entered('second', worktreeBranch='wt/second'))
        self.assertEqual(state.worktree.path, 'D:\\WebDev\\app-worktrees\\second')
        self.assertEqual(state.worktree.branch, 'wt/second')
        # Both launch directories, originalCwd first: they differ only in the
        # drive letter's case, which decides which one matches the folder slug.
        self.assertEqual(state.worktree.launch_cwds, ('D:\\WebDev\\app', 'd:\\WebDev\\app'))

    def test_leaving_the_worktree_clears_it(self) -> None:
        state = _ScanState()
        self._absorb(state, self._entered('first'))
        self._absorb(state, {'type': 'worktree-state', 'sessionId': 'x', 'worktreeSession': None})
        self.assertIsNone(state.worktree)

    def test_an_unexpected_shape_leaves_the_previous_one(self) -> None:
        state = _ScanState()
        self._absorb(state, self._entered('first'))
        for broken in ('nope', 42, {'worktreePath': 'D:\\x'}, {'originalCwd': 'D:\\x'}, {'originalCwd': 7, 'worktreePath': 'D:\\x'}):
            self._absorb(state, {'type': 'worktree-state', 'worktreeSession': broken})
        self._absorb(state, {'type': 'worktree-state'})
        self.assertEqual(state.worktree.path, 'D:\\WebDev\\app-worktrees\\first')

    def test_a_prompt_quoting_a_worktree_state_is_not_one(self) -> None:
        # The byte marker admits any line that mentions the type; only an entry
        # of that type may set the worktree.
        state = _ScanState()
        self._absorb(state, {'type': 'user', 'message': {'role': 'user', 'content': json.dumps(self._entered('quoted'))}})
        self.assertIsNone(state.worktree)

    def test_the_worktree_survives_a_partial_trailing_line(self) -> None:
        state = _ScanState()
        self._absorb(state, self._entered('first'))
        self.assertEqual(state.copy().worktree, state.worktree)

    def test_worktree_for_reports_it_and_warms_the_scan_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(json.dumps(self._entered('first')) + '\n', encoding='utf-8')
            tmod._scan_cache.clear()
            try:
                worktree = tmod.worktree_for(path)
                self.assertIn(os.path.normcase(str(path)), tmod._scan_cache)
            finally:
                tmod._scan_cache.clear()
            self.assertEqual(worktree.path, 'D:\\WebDev\\app-worktrees\\first')


class ModelTimelineOrderTest(unittest.TestCase):
    def test_sorts_chronologically_not_lexicographically(self) -> None:
        # '...07.500Z' is chronologically LATER than '...07Z' but sorts BEFORE it
        # as a raw string ('.' < 'Z'), so a lexicographic sort would name the
        # wrong model as current.
        timeline = _run_timeline([
            ('2026-07-11T10:53:07Z', 'opus'),
            ('2026-07-11T10:53:07.500Z', 'sonnet'),
        ], ('model',))
        self.assertEqual([entry['model'] for entry in timeline], ['opus', 'sonnet'])
        self.assertEqual(timeline[-1]['time'], '2026-07-11T10:53:07.500Z')

    def test_unparseable_timestamps_do_not_crash(self) -> None:
        timeline = _run_timeline([('not-a-timestamp', 'opus'), ('2026-07-11T10:00:00Z', 'sonnet')], ('model',))
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

        self.assertEqual(state.cli_events, [('2026-07-11T10:00:00Z', '2.1.224', '')])

    def test_events_carry_the_writers_entrypoint(self) -> None:
        # The entrypoint stamped on the entry says where the process that wrote
        # it ran - the one thing that makes two processes sharing a session
        # legible in the version history. A mistyped one is stored as none.
        state = _ScanState()
        for entry in (
            {'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'version': '2.1.241', 'entrypoint': 'claude-vscode',
             'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-5', 'usage': {}}},
            {'type': 'assistant', 'timestamp': '2026-07-11T10:00:30Z', 'version': '2.1.246', 'entrypoint': 'cli',
             'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-5', 'usage': {}}},
            {'type': 'assistant', 'timestamp': '2026-07-11T10:01:00Z', 'version': '2.1.246', 'entrypoint': 7,
             'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-5', 'usage': {}}},
        ):
            _absorb_line(json.dumps(entry).encode('utf-8'), state)

        self.assertEqual(state.cli_events, [
            ('2026-07-11T10:00:00Z', '2.1.241', 'claude-vscode'),
            ('2026-07-11T10:00:30Z', '2.1.246', 'cli'),
            ('2026-07-11T10:01:00Z', '2.1.246', ''),
        ])

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
        self.assertEqual(state.cli_events, [('2026-07-11T10:00:00Z', '2.1.228', '')])

    def test_timeline_compresses_runs_and_keeps_a_returned_version(self) -> None:
        # Same run-length compression as the model timeline: consecutive equal
        # versions collapse, and a downgrade back to an earlier version appears
        # again rather than being folded into its first run.
        timeline = _run_timeline([
            ('2026-07-11T09:00:00Z', '2.1.224'),
            ('2026-07-11T09:30:00Z', '2.1.224'),
            ('2026-07-11T11:00:00Z', '2.1.228'),
            ('2026-07-11T13:00:00Z', '2.1.224'),
        ], ('version',))
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
        ], ('version',))
        self.assertEqual([entry['version'] for entry in timeline], ['2.1.224', '2.1.228'])

    def test_timeline_breaks_a_run_on_a_change_of_writer(self) -> None:
        # Two processes holding one session write interleaved turns, each on
        # its own version and entrypoint: every handover starts a run, and so
        # does a handover on the same version. The on-disk order is resolved
        # first (the 09:03:16 turn sits after the 09:03:19 one on disk and still
        # collapses into the first run), and an entry without an entrypoint
        # reports none rather than an empty field.
        timeline = _run_timeline([
            ('2026-07-26T09:02:44Z', '2.1.241', 'claude-vscode'),
            ('2026-07-26T09:03:19Z', '2.1.246', 'cli'),
            ('2026-07-26T09:03:16Z', '2.1.241', 'claude-vscode'),
            ('2026-07-26T09:04:05Z', '2.1.241', 'claude-vscode'),
            ('2026-07-26T09:05:00Z', '2.1.241', 'cli'),
            ('2026-07-26T09:06:00Z', '2.1.241', ''),
        ], ('version', 'entrypoint'))
        self.assertEqual(timeline, [
            {'time': '2026-07-26T09:02:44Z', 'version': '2.1.241', 'entrypoint': 'claude-vscode'},
            {'time': '2026-07-26T09:03:19Z', 'version': '2.1.246', 'entrypoint': 'cli'},
            {'time': '2026-07-26T09:04:05Z', 'version': '2.1.241', 'entrypoint': 'claude-vscode'},
            {'time': '2026-07-26T09:05:00Z', 'version': '2.1.241', 'entrypoint': 'cli'},
            {'time': '2026-07-26T09:06:00Z', 'version': '2.1.241'},
        ])

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
                tmod.state_for(local_root(), session_id, cwd)

            self.assertEqual(len(calls), 1, 'a parseable tail must not escalate the read window')
        finally:
            tmod._scan_cache.clear()
            if previous is None:
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
            else:
                os.environ['CLAUDE_CONFIG_DIR'] = previous
            temp.cleanup()


class ActivityTimestampTest(unittest.TestCase):
    """Only a conversational turn's timestamp counts as session activity.

    Claude Code appends bookkeeping entries of its own (queue operations,
    file-history snapshots and deltas, the generated title).  They say nothing
    about the conversation having moved, so letting one set the activity
    timestamp would age a session backwards on housekeeping alone - the very
    thing preferring the entry timestamp over the file mtime is meant to avoid.
    """

    def test_bookkeeping_only_transcript_reports_no_activity(self) -> None:
        # What a session's own transcript holds while its whole turn runs inside
        # a subagent: metadata and nothing else.
        state = _parse(_lines(
            {'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': '2026-07-11T09:00:00Z'},
            {'type': 'file-history-snapshot', 'messageId': 'm1', 'snapshot': {'messageId': 'm1'}},
            {'type': 'ai-title', 'aiTitle': 'Generated title'},
            {'type': 'file-history-delta', 'messageId': 'm2', 'timestamp': '2026-07-11T09:10:00Z'},
        ))

        self.assertTrue(state.has_transcript)
        self.assertTrue(state.any_parsed)
        self.assertIsNone(state.last_entry_kind)
        self.assertIsNone(state.last_timestamp)

    def test_bookkeeping_after_a_turn_does_not_refresh_the_age(self) -> None:
        # A file-history delta lands whenever a tool writes a file - including a
        # subagent writing to the scratchpad - so it must not be mistaken for the
        # conversation itself moving on.
        state = _parse(_lines(
            {'type': 'assistant', 'timestamp': '2026-07-11T09:00:00Z',
             'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]}},
            {'type': 'file-history-delta', 'messageId': 'm2', 'timestamp': '2026-07-11T09:10:00Z'},
            {'type': 'queue-operation', 'operation': 'dequeue', 'timestamp': '2026-07-11T09:11:00Z'},
        ))

        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertEqual(state.last_timestamp, '2026-07-11T09:00:00Z')

    def test_every_conversational_kind_counts(self) -> None:
        for entry in (
            {'type': 'user', 'timestamp': '2026-07-11T09:00:05Z', 'message': {'role': 'user', 'content': 'hi'}},
            {'type': 'assistant', 'timestamp': '2026-07-11T09:00:05Z',
             'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}]}},
            {'type': 'system', 'subtype': 'local_command', 'timestamp': '2026-07-11T09:00:05Z'},
        ):
            with self.subTest(entry_type=entry['type']):
                state = _parse(_lines(
                    {'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'role': 'user', 'content': 'first'}},
                    entry,
                ))
                self.assertEqual(state.last_timestamp, '2026-07-11T09:00:05Z')

    def test_a_skipped_turn_still_does_not_count(self) -> None:
        # Sidechain and isMeta entries are skipped before the timestamp is read,
        # so a subagent's own turn cannot drive the main conversation's age.
        state = _parse(_lines(
            {'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'isSidechain': True, 'timestamp': '2026-07-11T09:05:00Z',
             'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'agent'}]}},
            {'type': 'user', 'isMeta': True, 'timestamp': '2026-07-11T09:06:00Z',
             'message': {'role': 'user', 'content': 'caveat'}},
        ))

        self.assertEqual(state.last_timestamp, '2026-07-11T09:00:00Z')


def _reply(**fields) -> dict:
    """A real assistant reply; *fields* override the entry, ``usage``/``content``/``model`` go into its message."""
    message = {
        'stop_reason': 'tool_use',
        'model': fields.pop('model', 'claude-opus-5-5'),
        'usage': fields.pop('usage', {'input_tokens': 5, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20}),
        'content': fields.pop('content', [{'type': 'text', 'text': 'x'}]),
    }
    entry = {'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z', 'message': message}
    entry.update(fields)
    return entry


def _plan_call(tool_input: dict) -> dict:
    return _reply(content=[{'type': 'tool_use', 'id': 'p', 'name': 'mcp__ccd_turn__update_plan', 'input': tool_input}])


def _plan_items(*statuses: str) -> dict:
    return _plan_call({'items': [{'content': 'step', 'status': status} for status in statuses]})


def _attachment(**attachment) -> dict:
    return {'type': 'attachment', 'timestamp': '2026-07-11T10:00:00Z', 'attachment': attachment}


class SessionDetailsTest(unittest.TestCase):
    """The display details the tail parser collects beside the status."""

    def test_the_newest_branch_of_any_main_entry_wins(self) -> None:
        state = _parse(_lines(
            {**_reply(), 'gitBranch': 'main'},
            {**_attachment(type='date'), 'gitBranch': 'feature/x'},
            {**_reply(), 'gitBranch': 'side', 'isSidechain': True},
            {**_reply(), 'gitBranch': 42},
        ))
        self.assertEqual(state.git_branch, 'feature/x')

    def test_effort_and_speed_come_from_the_newest_real_reply(self) -> None:
        state = _parse(_lines(
            _reply(effort='high', usage={'input_tokens': 1, 'speed': 'fast'}),
            _reply(effort='Not A Token'),
            _reply(model='<synthetic>', effort='max', usage={'input_tokens': 0, 'speed': 'slow'}),
        ))
        # The synthetic turn's effort still counts - it is the entry's own field -
        # but its usage is no real request, so the speed stays the real one.
        self.assertEqual(state.effort, 'max')
        self.assertEqual(state.speed, 'fast')

    def test_an_api_error_turn_sets_no_detail(self) -> None:
        state = _parse(_lines(_reply(effort='low'), {**_reply(effort='max'), 'isApiErrorMessage': True}))
        self.assertEqual(state.effort, 'low')

    def test_context_is_what_the_newest_reply_sent(self) -> None:
        state = _parse(_lines(
            _reply(usage={'input_tokens': 1, 'cache_read_input_tokens': 2}),
            _reply(model='claude-sonnet-5-5', usage={'input_tokens': 5, 'cache_read_input_tokens': 400000,
                                                      'cache_creation_input_tokens': 1000, 'output_tokens': 9000}),
            _reply(model='<synthetic>', usage={'input_tokens': 0}),
        ))
        self.assertEqual(state.context_tokens, 401005)
        self.assertEqual(state.context_model, 'claude-sonnet-5-5')

    def test_the_cache_lapses_one_lifetime_after_the_newest_reply_that_used_it(self) -> None:
        state = _parse(_lines(
            _reply(usage={'cache_creation_input_tokens': 500, 'cache_creation': {'ephemeral_1h_input_tokens': 500}}),
            # Only read: the lifetime the write set stands, the clock restarts.
            _reply(timestamp='2026-07-11T10:20:00Z', usage={'input_tokens': 3, 'cache_read_input_tokens': 500}),
        ))
        self.assertEqual(state.cache_ttl_seconds, 3600)
        self.assertEqual(state.cache_expires_at, _timestamp_epoch('2026-07-11T10:20:00Z') + 3600)

    def test_the_lifetime_that_wrote_more_tokens_decides(self) -> None:
        state = _parse(_lines(_reply(usage={
            'cache_creation_input_tokens': 900,
            'cache_creation': {'ephemeral_5m_input_tokens': 800, 'ephemeral_1h_input_tokens': 100},
        })))
        self.assertEqual(state.cache_ttl_seconds, 300)

    def test_without_a_known_lifetime_the_cache_is_not_reported(self) -> None:
        # A reply that only read, one whose split is mistyped, and one whose split
        # names a lifetime but whose token counts say no cache was used leave
        # nothing to count down.
        for usage in (
            {'input_tokens': 3, 'cache_read_input_tokens': 500},
            {'cache_creation_input_tokens': 5, 'cache_creation': {'ephemeral_1h_input_tokens': True, 'ephemeral_5m_input_tokens': '7'}},
            {'input_tokens': 9, 'cache_creation': {'ephemeral_1h_input_tokens': 4}},
        ):
            with self.subTest(usage=usage):
                state = _parse(_lines(_reply(usage=usage)))
                self.assertIsNone(state.cache_ttl_seconds)
                self.assertIsNone(state.cache_expires_at)

    def test_a_reply_without_usage_leaves_the_context_unknown(self) -> None:
        state = _parse(_lines(_reply(usage={'input_tokens': True, 'cache_read_input_tokens': -4})))
        self.assertIsNone(state.context_tokens)

    def test_the_newest_valid_budget_is_reported(self) -> None:
        state = _parse(_lines(
            _attachment(type='budget_usd', used=2.5, total=20, remaining=17.5),
            _attachment(type='budget_usd', used=True, total=60),
            _attachment(type='budget_usd', used=1, total=0),
        ))
        self.assertEqual((state.budget_used, state.budget_total), (2.5, 20.0))

    def test_queued_prompts_are_counted_and_never_go_below_zero(self) -> None:
        def operation(name: str) -> dict:
            return {'type': 'queue-operation', 'operation': name, 'timestamp': '2026-07-11T10:00:00Z'}

        state = _parse(_lines(operation('dequeue'), operation('enqueue'), operation('enqueue'), operation('remove'),
                              operation('enqueue'), operation('unknown')))
        self.assertEqual(state.queued, 2)

    def test_a_plan_reports_its_step_in_progress(self) -> None:
        state = _parse(_lines(_plan_items('completed', 'in_progress', 'pending', 'pending')))
        self.assertEqual((state.plan_step, state.plan_total), (2, 4))

    def test_advance_moves_on_by_one_step(self) -> None:
        state = _parse(_lines(_plan_items('in_progress', 'pending', 'pending'), _plan_call({'advance': True})))
        self.assertEqual(state.plan_step, 2)

    def test_done_and_start_are_one_based(self) -> None:
        state = _parse(_lines(_plan_items('in_progress', 'pending', 'pending', 'pending'),
                              _plan_call({'done': [1, 2], 'start': 3})))
        self.assertEqual((state.plan_step, state.plan_total), (3, 4))

    def test_out_of_range_steps_are_ignored(self) -> None:
        state = _parse(_lines(_plan_items('in_progress', 'pending'), _plan_call({'done': [0, 9, True], 'start': 7})))
        self.assertEqual(state.plan_step, 1)

    def test_a_finished_plan_reports_nothing(self) -> None:
        state = _parse(_lines(_plan_items('completed', 'in_progress'), _plan_call({'advance': True})))
        self.assertIsNone(state.plan_step)
        self.assertEqual(state.plan_total, 0)

    def test_failing_hooks_are_grouped_by_name_and_exit_code(self) -> None:
        entries = [_attachment(type='hook_non_blocking_error', hookName='PostToolUse:Bash', exitCode=7)] * 3
        entries += [_attachment(type='hook_non_blocking_error', hookName='UserPromptSubmit', exitCode=1)]
        entries += [_attachment(type='hook_non_blocking_error', hookName='PostToolUse:Bash', exitCode='x')]
        entries += [_attachment(type='hook_non_blocking_error', hookName=f'Hook{index}', exitCode=1) for index in range(6)]
        state = _parse(_lines(*entries))

        # Most frequent first, ties in the order first seen; a mistyped exit code
        # is unknown rather than dropped.
        self.assertEqual(state.hook_errors[:3], (
            tmod.HookError('PostToolUse:Bash', 7, 3),
            tmod.HookError('UserPromptSubmit', 1, 1),
            tmod.HookError('PostToolUse:Bash', None, 1),
        ))
        self.assertEqual(len(state.hook_errors), 5)

    def test_the_artifact_count_is_the_newest(self) -> None:
        state = _parse(_lines({'type': 'frame-link', 'artifactCount': 1}, {'type': 'frame-link', 'artifactCount': 3},
                              {'type': 'frame-link', 'artifactCount': -1}))
        self.assertEqual(state.artifact_count, 3)

    def test_pull_requests_are_listed_newest_first_once_each(self) -> None:
        def link(number: object, url: object = 'https://git.example/r/-/merge_requests/1') -> dict:
            return {'type': 'pr-link', 'prRepository': 'group/repo', 'prNumber': number, 'prUrl': url}

        # The address is judged by the UI, where it would reach an href; here it
        # is only required to be a string of a sane length.
        state = _parse(_lines(link(1), link(2, 12345), link(3, 'x' * 401), link(1), link(0), link('4')))
        self.assertEqual(state.pr_links, (
            tmod.PullRequest('group/repo', 1, 'https://git.example/r/-/merge_requests/1'),
            tmod.PullRequest('group/repo', 3, None),
            tmod.PullRequest('group/repo', 2, None),
        ))

    def test_at_most_the_newest_pull_requests_are_reported(self) -> None:
        state = _parse(_lines(*({'type': 'pr-link', 'prRepository': 'r', 'prNumber': number} for number in range(1, 9))))
        self.assertEqual([link.number for link in state.pr_links], [8, 7, 6, 5, 4])


class ScanDetailsTest(unittest.TestCase):
    """The session-wide counts of the full scan: files edited and context compactions."""

    @staticmethod
    def _absorb(state, entry):
        _absorb_line(json.dumps(entry).encode('utf-8'), state)

    @staticmethod
    def _tool(name: str, **tool_input) -> dict:
        return {'type': 'tool_use', 'id': name, 'name': name, 'input': tool_input}

    def test_distinct_files_written_are_counted(self) -> None:
        state = _ScanState()
        self._absorb(state, _reply(content=[self._tool('Edit', file_path='a.py'), self._tool('Write', file_path='b.py')]))
        self._absorb(state, _reply(content=[self._tool('MultiEdit', file_path='a.py'), self._tool('NotebookEdit', notebook_path='c.ipynb'),
                                           self._tool('Read', file_path='d.py'), self._tool('Edit', file_path=7)]))
        self.assertEqual(state.edited_files, {'a.py', 'b.py', 'c.ipynb'})

    def test_compactions_of_the_main_conversation_are_counted(self) -> None:
        state = _ScanState()
        self._absorb(state, {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': {'trigger': 'auto'}})
        self._absorb(state, {'type': 'system', 'subtype': 'compact_boundary', 'isSidechain': True})
        self._absorb(state, {'type': 'system', 'subtype': 'informational', 'content': 'compact_boundary'})
        self.assertEqual(state.compactions, 1)

    def test_the_counts_survive_a_partial_trailing_line(self) -> None:
        state = _ScanState()
        self._absorb(state, _reply(content=[self._tool('Edit', file_path='a.py')]))
        self._absorb(state, {'type': 'system', 'subtype': 'compact_boundary'})
        copy = state.copy()
        self.assertEqual((copy.edited_files, copy.compactions), ({'a.py'}, 1))
        copy.edited_files.add('b.py')
        self.assertEqual(state.edited_files, {'a.py'})

    def test_artifacts_are_collected_from_the_entry_that_names_them(self) -> None:
        # Claude Code names an artifact once; every later frame-link carries the
        # count alone and must not wipe the title.
        state = _ScanState()
        self._absorb(state, {'type': 'frame-link', 'artifactCount': 1, 'title': 'Concepts', 'frameUrl': 'https://claude.ai/artifact/a1'})
        self._absorb(state, {'type': 'frame-link', 'artifactCount': 1})
        self._absorb(state, {'type': 'frame-link', 'artifactCount': 2, 'title': 'Spec', 'frameUrl': 'https://claude.ai/artifact/b2'})
        self._absorb(state, {'type': 'frame-link', 'artifactCount': 2, 'frameUrl': 'https://claude.ai/artifact/a1'})
        self._absorb(state, {'type': 'frame-link', 'artifactCount': 2, 'title': 'Spec v2', 'frameUrl': 'https://claude.ai/artifact/b2'})
        self.assertEqual(state.artifacts, {'https://claude.ai/artifact/a1': 'Concepts', 'https://claude.ai/artifact/b2': 'Spec v2'})

    def test_an_address_that_is_not_https_is_ignored(self) -> None:
        state = _ScanState()
        for url in ('http://claude.ai/artifact/x', 'javascript:alert(1)', 7, '', 'https://' + 'x' * 500):
            self._absorb(state, {'type': 'frame-link', 'artifactCount': 1, 'title': 'T', 'frameUrl': url})
        self.assertEqual(state.artifacts, {})

    def test_only_the_newest_artifacts_are_kept(self) -> None:
        state = _ScanState()
        for index in range(tmod._MAX_ARTIFACTS + 3):
            self._absorb(state, {'type': 'frame-link', 'title': f'A{index}', 'frameUrl': f'https://claude.ai/artifact/{index}'})
        self.assertEqual(len(state.artifacts), tmod._MAX_ARTIFACTS)
        self.assertEqual(next(iter(state.artifacts.values())), 'A3')

    def test_a_scan_reports_the_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(json.dumps({'type': 'frame-link', 'artifactCount': 1, 'title': 'Concepts',
                                        'frameUrl': 'https://claude.ai/artifact/a1'}) + '\n', encoding='utf-8')
            tmod._scan_cache.clear()
            try:
                result = tmod._scan_appended(path)
            finally:
                tmod._scan_cache.clear()
        self.assertEqual(result.artifacts, (tmod.Artifact('Concepts', 'https://claude.ai/artifact/a1'),))

    def test_a_scan_reports_the_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(''.join(json.dumps(entry) + '\n' for entry in (
                _reply(content=[self._tool('Edit', file_path='a.py')]),
                {'type': 'system', 'subtype': 'compact_boundary'},
            )), encoding='utf-8')
            tmod._scan_cache.clear()
            try:
                result = tmod._scan_appended(path)
            finally:
                tmod._scan_cache.clear()
        self.assertEqual((result.files_edited, result.compactions), (1, 1))



def _assistant(content: list, stop_reason: str = 'tool_use', timestamp: str = '2026-07-11T10:00:00Z') -> dict:
    # Every real reply carries its usage, which is what the full scan's pre-filter keys on.
    return {'type': 'assistant', 'timestamp': timestamp, 'message': {
        'stop_reason': stop_reason, 'model': 'claude-opus-5-5', 'content': content, 'usage': {'input_tokens': 1, 'output_tokens': 1}}}


def _tool_use(name: str, tool_id: str = 't1', **tool_input) -> dict:
    return {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': tool_input}


def _prompt(text: str, timestamp: str) -> dict:
    return {'type': 'user', 'timestamp': timestamp, 'message': {'role': 'user', 'content': text}}


class CurrentToolTest(unittest.TestCase):
    """The call a session runs or waits on, described by its own subject."""

    def test_a_pending_command_names_itself(self) -> None:
        state = _parse(_lines(_assistant([_tool_use('Bash', command='npm test', description='Run the tests')])))
        self.assertEqual(state.current_tool, tmod.ToolCall('Bash', 'npm test', 'Run the tests'))
        self.assertEqual(state.current_tool_at, _timestamp_epoch('2026-07-11T10:00:00Z'))

    def test_an_answered_call_is_not_current(self) -> None:
        state = _parse(_lines(
            _assistant([_tool_use('Bash', command='npm test')]),
            {'type': 'user', 'timestamp': '2026-07-11T10:00:05Z', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
        ))
        self.assertIsNone(state.current_tool)
        self.assertIsNone(state.current_tool_at)

    def test_a_question_dialog_carries_its_questions_and_answers(self) -> None:
        questions = [{'question': 'Which layout?', 'header': 'Layout', 'multiSelect': False,
                      'options': [{'label': 'Grid', 'description': 'd'}, {'label': 'Board', 'description': 'd'}]}]
        state = _parse(_lines(_assistant([_tool_use('AskUserQuestion', questions=questions)])))
        self.assertEqual(state.current_tool.questions, (tmod.Question('Which layout?', ('Grid', 'Board')),))

    def test_a_plan_to_review_carries_the_plan_cut_to_length(self) -> None:
        state = _parse(_lines(_assistant([_tool_use('ExitPlanMode', plan='x' * 9000)])))
        self.assertEqual(len(state.current_tool.text), tmod._PLAN_TEXT_MAX_CHARS)

    def test_a_file_call_names_the_file_and_an_unknown_call_its_first_text(self) -> None:
        state = _parse(_lines(_assistant([_tool_use('Edit', file_path='src/app.py', old_string='a', new_string='b')])))
        self.assertEqual(state.current_tool.text, 'src/app.py')
        state = _parse(_lines(_assistant([_tool_use('mcp__x__do', target='the thing', count=3)])))
        self.assertEqual(state.current_tool.text, 'the thing')


class LastReplyTest(unittest.TestCase):
    def test_the_newest_reply_text_is_kept(self) -> None:
        state = _parse(_lines(
            _assistant([{'type': 'text', 'text': 'First answer'}], 'end_turn'),
            _assistant([{'type': 'thinking', 'thinking': 'hidden'}, {'type': 'text', 'text': '  Done: all green.  '}], 'end_turn'),
        ))
        self.assertEqual(state.last_reply, 'Done: all green.')

    def test_a_reply_without_text_keeps_the_previous_one(self) -> None:
        state = _parse(_lines(
            _assistant([{'type': 'text', 'text': 'Answer'}], 'end_turn'),
            _assistant([_tool_use('Bash', command='ls')]),
        ))
        self.assertEqual(state.last_reply, 'Answer')


class PlanItemsAndHookMessagesTest(unittest.TestCase):
    def test_plan_steps_carry_their_text(self) -> None:
        items = [{'content': 'Read the code', 'status': 'completed'}, {'content': 'Write the fix', 'status': 'in_progress'}]
        state = _parse(_lines(_assistant([_tool_use(tmod._PLAN_TOOL, items=items)])))
        self.assertEqual(state.plan_items, (tmod.PlanItem('Read the code', 'completed'), tmod.PlanItem('Write the fix', 'in_progress')))

    def test_a_failing_hook_reports_its_first_error_line(self) -> None:
        attachment = {'type': 'hook_non_blocking_error', 'hookName': 'PostToolUse:Bash', 'exitCode': 7,
                      'stderr': '\n  lint failed: 3 errors\nsecond line', 'stdout': ''}
        state = _parse(_lines({'type': 'attachment', 'attachment': attachment}))
        self.assertEqual(state.hook_errors[0].message, 'lint failed: 3 errors')

    def test_claude_codes_own_hook_wording_is_no_message(self) -> None:
        def message(stderr: str, stdout: str = '') -> str:
            attachment = {'type': 'hook_non_blocking_error', 'hookName': 'Stop', 'exitCode': 1, 'stderr': stderr, 'stdout': stdout}
            return _parse(_lines({'type': 'attachment', 'attachment': attachment})).hook_errors[0].message

        self.assertEqual(message('Failed with non-blocking status code: No stderr output'), '')
        self.assertEqual(message('Failed with non-blocking status code: eslint found 2 errors'), 'eslint found 2 errors')
        self.assertEqual(message('', 'stdout is never read'), '')


class SessionStepsTest(unittest.TestCase):
    """A session's own newest steps, read from the end of its transcript."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'session.jsonl'

    def _write(self, entries: list[dict]) -> None:
        self.path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')

    def test_the_whole_file_when_it_fits_without_the_sidechain(self) -> None:
        self._write([
            _assistant([{'type': 'tool_use', 'id': 'u1', 'name': 'Bash', 'input': {'command': 'ls'}}], 'tool_use', '2026-07-11T10:00:00Z'),
            {'type': 'user', 'timestamp': '2026-07-11T10:00:01Z', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1', 'content': 'a.txt'}]}},
            {**_assistant([{'type': 'tool_use', 'id': 's1', 'name': 'Read', 'input': {}}], 'tool_use', '2026-07-11T10:00:02Z'), 'isSidechain': True},
        ])

        steps, partial = tmod.session_steps(self.path)

        self.assertFalse(partial)
        self.assertEqual([(step['tool'], step['result']) for step in steps], [('Bash', 'a.txt')])

    def test_a_long_transcript_yields_its_newest_steps_only(self) -> None:
        entries = []
        for index in range(200):
            entries.append(_assistant([{'type': 'tool_use', 'id': f'u{index}', 'name': 'Bash', 'input': {'command': 'x' * 200}}],
                                      'tool_use', '2026-07-11T10:00:00Z'))
        self._write(entries)

        steps, partial = tmod.session_steps(self.path, tail_bytes=4096)

        self.assertTrue(partial)
        self.assertTrue(0 < len(steps) < 200)
        self.assertEqual(steps[-1]['id'], 'u199')

    def test_a_missing_file_has_no_steps(self) -> None:
        self.assertEqual(tmod.session_steps(self.path), ([], False))


class AgentStepsIncrementalTest(unittest.TestCase):
    """A running agent's steps: only what was appended is read on the next call."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'agent-a1.jsonl'
        tmod._steps_cache.clear()
        self.addCleanup(tmod._steps_cache.clear)

    def _append(self, text: str) -> None:
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(text)

    @staticmethod
    def _call(tool_use_id: str) -> str:
        return json.dumps(_assistant([{'type': 'tool_use', 'id': tool_use_id, 'name': 'Bash', 'input': {}}], 'tool_use', '2026-07-11T10:00:00Z'))

    @staticmethod
    def _result(tool_use_id: str, text: str) -> str:
        return json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': text}]}})

    def test_appended_lines_add_steps_and_fill_in_earlier_results(self) -> None:
        self._append(self._call('u1') + '\n')
        first = tmod.agent_steps(self.path)
        self._append(self._result('u1', 'done') + '\n' + self._call('u2') + '\n')

        second = tmod.agent_steps(self.path)

        self.assertEqual([(step['id'], step['result']) for step in first], [('u1', None)])
        self.assertEqual([(step['id'], step['result']) for step in second], [('u1', 'done'), ('u2', None)])

    def test_a_line_still_being_written_waits_for_the_next_call(self) -> None:
        line = self._call('u1')
        self._append(line[:20])
        self.assertEqual(tmod.agent_steps(self.path), [])
        self._append(line[20:] + '\n')

        self.assertEqual([step['id'] for step in tmod.agent_steps(self.path)], ['u1'])

    def test_a_file_that_shrank_is_read_afresh(self) -> None:
        self._append(self._call('u1') + '\n' + self._call('u2') + '\n')
        tmod.agent_steps(self.path)
        self.path.write_text(self._call('u3') + '\n', encoding='utf-8')

        self.assertEqual([step['id'] for step in tmod.agent_steps(self.path)], ['u3'])


class ScanMonitoringTest(unittest.TestCase):
    """Lines changed, worked intervals and messages, from the full scan."""

    @staticmethod
    def _absorb(state, entry):
        _absorb_line(json.dumps(entry).encode('utf-8'), state)

    def test_edits_and_subagents_add_their_lines(self) -> None:
        state = _ScanState()
        patch = [{'lines': ['+a', '+b', '-c', ' d']}, {'lines': ['-e']}]
        self._absorb(state, {'type': 'user', 'toolUseResult': {'type': 'update', 'structuredPatch': patch},
                             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}})
        self._absorb(state, {'type': 'user', 'toolUseResult': {'type': 'create', 'structuredPatch': [], 'content': 'x\ny\nz'},
                             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't2'}]}})
        self._absorb(state, {'type': 'user', 'toolUseResult': {'toolStats': {'linesAdded': 10, 'linesRemoved': 4}},
                             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't3'}]}})
        self.assertEqual((state.lines_added, state.lines_removed), (2 + 3 + 10, 2 + 4))

    def test_a_turn_from_prompt_to_its_end_is_one_worked_interval(self) -> None:
        state = _ScanState()
        self._absorb(state, _prompt('Fix it', '2026-07-11T10:00:00Z'))
        self._absorb(state, {'type': 'user', 'timestamp': '2026-07-11T10:00:30Z',
                             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}})
        self._absorb(state, _assistant([{'type': 'text', 'text': 'done'}], 'end_turn', '2026-07-11T10:02:00Z'))
        self._absorb(state, _assistant([{'type': 'text', 'text': 'stray'}], 'end_turn', '2026-07-11T10:03:00Z'))
        start, end = _timestamp_epoch('2026-07-11T10:00:00Z'), _timestamp_epoch('2026-07-11T10:02:00Z')
        self.assertEqual(state.intervals, [(start, end)])
        self.assertIsNone(state.turn_started_at)

    def test_an_interrupt_or_an_api_error_ends_the_interval(self) -> None:
        state = _ScanState()
        self._absorb(state, _prompt('Fix it', '2026-07-11T10:00:00Z'))
        self._absorb(state, {'type': 'user', 'timestamp': '2026-07-11T10:01:00Z',
                             'message': {'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}})
        self._absorb(state, _prompt('Again', '2026-07-11T11:00:00Z'))
        error = _assistant([{'type': 'text', 'text': 'API Error'}], 'stop_sequence', '2026-07-11T11:00:20Z')
        error['isApiErrorMessage'] = True
        self._absorb(state, error)
        epoch = _timestamp_epoch
        self.assertEqual(state.intervals, [(epoch('2026-07-11T10:00:00Z'), epoch('2026-07-11T10:01:00Z')),
                                           (epoch('2026-07-11T11:00:00Z'), epoch('2026-07-11T11:00:20Z'))])

    def test_a_local_command_does_not_stay_a_worked_turn(self) -> None:
        state = _ScanState()
        self._absorb(state, _prompt('<command-name>/model</command-name>', '2026-07-11T10:00:00Z'))
        self._absorb(state, {'type': 'user', 'timestamp': '2026-07-11T10:00:01Z',
                             'message': {'content': '<local-command-stdout>Set model</local-command-stdout>'}})
        self.assertIsNone(state.turn_started_at)
        self._absorb(state, _prompt('Fix it', '2026-07-11T14:00:00Z'))
        self._absorb(state, _assistant([{'type': 'text', 'text': 'done'}], 'end_turn', '2026-07-11T14:01:00Z'))
        epoch = _timestamp_epoch
        self.assertEqual(state.intervals, [(epoch('2026-07-11T10:00:00Z'), epoch('2026-07-11T10:00:01Z')),
                                           (epoch('2026-07-11T14:00:00Z'), epoch('2026-07-11T14:01:00Z'))])

    def test_an_unfinished_turn_stays_open(self) -> None:
        state = _ScanState()
        self._absorb(state, _prompt('Fix it', '2026-07-11T10:00:00Z'))
        self.assertEqual(state.turn_started_at, _timestamp_epoch('2026-07-11T10:00:00Z'))
        self.assertEqual(state.intervals, [])

    def test_messages_sent_and_received_are_logged(self) -> None:
        state = _ScanState()
        self._absorb(state, _assistant([_tool_use('SendMessage', to='review-session', message='hi')], 'tool_use', '2026-07-11T10:00:00Z'))
        self._absorb(state, {'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'message': {'content': 'report'},
                             'origin': {'kind': 'peer', 'from': 'a1b2', 'name': 'Explore', 'handback': True, 'body': 'long'}})
        self.assertEqual(state.messages, [
            tmod.MessageEvent('sent', 'review-session', _timestamp_epoch('2026-07-11T10:00:00Z')),
            tmod.MessageEvent('received', 'a1b2', _timestamp_epoch('2026-07-11T10:01:00Z'), 'Explore', True),
        ])

    def test_a_message_queued_while_a_turn_ran_is_logged_as_received(self) -> None:
        state = _ScanState()
        origin = {'kind': 'peer', 'from': r'uds:\\.\pipe\LOCAL\cc-msg-1', 'msg_id': 'm1', 'name': 'Photo editor', 'body': 'long'}
        # Written compact, as Claude Code writes it: the pre-filter looks for the peer marker without spaces.
        for entry in (
            {'type': 'attachment', 'timestamp': '2026-07-11T10:00:00Z', 'attachment': {'type': 'queued_command', 'prompt': 'long', 'origin': origin}},
            {'type': 'attachment', 'timestamp': '2026-07-11T10:01:00Z', 'isSidechain': True, 'attachment': {'type': 'queued_command', 'origin': origin}},
            {'type': 'attachment', 'timestamp': '2026-07-11T10:02:00Z', 'attachment': {'type': 'deferred_tools_delta', 'origin': origin}},
        ):
            _absorb_line(json.dumps(entry, separators=(',', ':')).encode('utf-8'), state)
        self.assertEqual(state.messages, [
            tmod.MessageEvent('received', origin['from'], _timestamp_epoch('2026-07-11T10:00:00Z'), 'Photo editor'),
        ])

    def test_a_background_agents_task_notification_is_its_hand_back(self) -> None:
        state = _ScanState()
        compact = (lambda entry: _absorb_line(json.dumps(entry, separators=(',', ':')).encode('utf-8'), state))
        origin = {'kind': 'task-notification', 'producer': 'session-task'}

        def notification(task_id, status):
            return (f'<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n'
                    f'<status>{status}</status>\n<summary>Agent done <task-id>a999999999999999</task-id></summary>\n<result>words</result>')

        compact({'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'origin': origin,
                 'message': {'content': notification('aa256c3745ca9080f', 'completed')}})
        compact({'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'origin': origin,
                 'message': {'content': [{'type': 'text', 'text': notification('ab0123456789abcd', 'killed')}]}})
        compact({'type': 'attachment', 'timestamp': '2026-07-11T10:02:00Z',
                 'attachment': {'type': 'queued_command', 'prompt': notification('ac0123456789abcd', 'weird'), 'origin': origin}})
        # A shell task's notification names no agent, and a prompt of another origin is no report.
        compact({'type': 'user', 'timestamp': '2026-07-11T10:03:00Z', 'origin': origin,
                 'message': {'content': notification('b9k2t2axg', 'completed')}})
        compact({'type': 'user', 'timestamp': '2026-07-11T10:04:00Z', 'origin': {'kind': 'human'},
                 'message': {'content': notification('ad0123456789abcd', 'completed')}})

        self.assertEqual(state.messages, [
            tmod.MessageEvent('received', 'aa256c3745ca9080f', _timestamp_epoch('2026-07-11T10:00:00Z'), '', True, 'completed'),
            tmod.MessageEvent('received', 'ab0123456789abcd', _timestamp_epoch('2026-07-11T10:01:00Z'), '', True, 'killed'),
            tmod.MessageEvent('received', 'ac0123456789abcd', _timestamp_epoch('2026-07-11T10:02:00Z'), '', True, ''),
        ])

    def test_how_each_subagent_was_started_is_recorded_by_its_id(self) -> None:
        state = _ScanState()
        compact = (lambda entry: _absorb_line(json.dumps(entry, separators=(',', ':')).encode('utf-8'), state))
        calls = [
            {'type': 'tool_use', 'id': 't1', 'name': 'Agent', 'input': {'prompt': 'p', 'run_in_background': True, 'effort': 'high'}},
            {'type': 'tool_use', 'id': 't2', 'name': 'Task', 'input': {'prompt': 'p', 'effort': 'not a token!'}},
        ]
        compact(_assistant(calls, 'tool_use', '2026-07-11T10:00:00Z'))

        def result(tool_use_id, **fields):
            return {'type': 'user', 'timestamp': '2026-07-11T10:00:01Z', 'isSidechain': False, 'toolUseResult': fields,
                    'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': 'x'}]}}

        compact(result('t1', status='async_launched', agentId='a-bg', isAsync=True))
        compact(result('t2', status='completed', agentId='a-fg'))
        compact(result('t9', status='forked', agentId='a-skill', commandName='commit'))
        compact(result('t3', status='completed', agentId=7))

        self.assertEqual(state.agent_launches, {
            'a-bg': tmod.AgentLaunch('background', 'high'),
            'a-fg': tmod.AgentLaunch('foreground', ''),
            'a-skill': tmod.AgentLaunch('skill'),
        })
        self.assertEqual(state.pending_launches, {})

    def test_an_agent_file_scan_says_when_it_started_what_it_called_and_changed(self) -> None:
        state = _ScanState(first_prompt='')
        lines = [
            {'type': 'user', 'isSidechain': True, 'agentId': 'a1', 'timestamp': '2026-07-11T10:00:00Z', 'message': {'content': 'task'}},
            {'type': 'assistant', 'isSidechain': True, 'agentId': 'a1', 'timestamp': '2026-07-11T10:00:05Z',
             'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'stop_reason': 'tool_use', 'usage': {'output_tokens': 3},
                         'content': [{'type': 'tool_use', 'id': 'u1', 'name': 'Edit', 'input': {'file_path': 'x'}},
                                     {'type': 'tool_use', 'id': 'u2', 'name': 'Read', 'input': {}}]}},
            {'type': 'user', 'isSidechain': True, 'agentId': 'a1', 'timestamp': '2026-07-11T10:00:06Z',
             'toolUseResult': {'structuredPatch': [{'lines': ['+a', '+b', '-c']}]},
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1'}]}},
        ]
        for entry in lines:
            _absorb_line(json.dumps(entry, separators=(',', ':')).encode('utf-8'), state)

        usage = state.usage()
        self.assertEqual(usage.started_at, _timestamp_epoch('2026-07-11T10:00:00Z'))
        self.assertEqual((usage.tool_calls, usage.lines_added, usage.lines_removed), (2, 2, 1))
        # A subagent's own lines are not the main conversation's.
        self.assertEqual((state.lines_added, state.lines_removed, state.agent_launches), (0, 0, {}))

    def test_each_tool_called_is_counted_by_name_with_its_last_use(self) -> None:
        state = _ScanState()
        calls = [
            {'type': 'tool_use', 'id': 'u1', 'name': 'Bash', 'input': {'command': 'ls'}},
            {'type': 'tool_use', 'id': 'u2', 'name': 'mcp__discord__read_messages', 'input': {}},
            {'type': 'tool_use', 'id': 'u3', 'name': 'not a name!', 'input': {}},
        ]
        self._absorb(state, _assistant(calls, 'tool_use', '2026-07-11T10:00:00Z'))
        self._absorb(state, _assistant([{'type': 'tool_use', 'id': 'u4', 'name': 'Bash', 'input': {}}], 'tool_use', '2026-07-11T09:00:00Z'))

        self.assertEqual(state.tool_calls, 4)
        self.assertEqual(tmod._tool_use_tuples(state.tool_uses), {
            'Bash': (2, _timestamp_epoch('2026-07-11T10:00:00Z'), 0),
            'mcp__discord__read_messages': (1, _timestamp_epoch('2026-07-11T10:00:00Z'), 0),
        })
        self.assertEqual(state.usage().tools['Bash'][0], 2)

    def test_a_failed_tool_call_counts_against_its_tool_but_a_rejected_one_does_not(self) -> None:
        state = _ScanState()
        calls = [{'type': 'tool_use', 'id': f'u{index}', 'name': 'Bash', 'input': {}} for index in range(6)]
        self._absorb(state, _assistant(calls, 'tool_use', '2026-07-11T10:00:00Z'))

        def result(tool_use_id, text, is_error):
            return {'type': 'user', 'timestamp': '2026-07-11T10:00:01Z', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': tool_use_id, 'is_error': is_error, 'content': text}]}}

        # Written compact, as Claude Code writes it: the pre-filter looks for the error marker without spaces.
        for entry in (result('u0', 'Exit code 1\nboom', True), result('u1', 'fine', False),
                      result('u2', "The user doesn't want to proceed with this tool use.", True),
                      result('u3', [{'type': 'text', 'text': 'Exit code 2'}], True), result('u9', 'unknown call', True),
                      result('u4', 'Permission for this command was denied by a rule.', True),
                      result('u5', '[Tool call interrupted: the session ended before it finished]', True)):
            _absorb_line(json.dumps(entry, separators=(',', ':')).encode('utf-8'), state)

        self.assertEqual(state.tool_uses['Bash'][2], 2)

    def test_every_worktree_entered_is_remembered_once(self) -> None:
        state = _ScanState()
        for path in ('D:\\repo-worktrees\\one', 'D:\\repo-worktrees\\two', 'D:\\repo-worktrees\\one'):
            self._absorb(state, {'type': 'worktree-state', 'worktreeSession': {'originalCwd': 'D:\\repo', 'worktreePath': path}})
        self._absorb(state, {'type': 'worktree-state', 'worktreeSession': None})
        self.assertEqual(state.worktree_paths, ['D:\\repo-worktrees\\one', 'D:\\repo-worktrees\\two'])
        self.assertIsNone(state.worktree)


if __name__ == '__main__':
    unittest.main()
