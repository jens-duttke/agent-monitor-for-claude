"""
Tests for transcript parsing and the privacy boundary.

The privacy tests are the structural guarantee that conversation content -
message text, thinking blocks, tool inputs, and tool results - never leaves
the parser or reaches the snapshot the UI receives.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from agent_monitor_for_claude.paths import transcript_path
from agent_monitor_for_claude.snapshot import build_snapshot
from agent_monitor_for_claude.transcript import history_state_for, state_for

# Markers placed in every content-bearing field of the synthetic transcript.
# None of them may appear in parsed metadata or the rendered snapshot.
_SECRETS = ('SECRET_TEXT', 'SECRET_THINKING', 'SECRET_INPUT', 'SECRET_RESULT')

_SESSION_ID = 'privacy-session-id'
_CWD = 'c:\\Temp\\privacy-proj'


def _transcript_lines() -> list[str]:
    """A transcript whose content fields all carry secret markers."""
    return [
        json.dumps({'type': 'ai-title', 'aiTitle': 'Session title label', 'sessionId': _SESSION_ID}),
        json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T10:53:07Z',
            'message': {
                'stop_reason': 'tool_use',
                'model': 'claude-opus-4-8[1m]',
                'usage': {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 200},
                'content': [
                    {'type': 'thinking', 'thinking': 'SECRET_THINKING'},
                    {'type': 'text', 'text': 'SECRET_TEXT'},
                    {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'SECRET_INPUT'}},
                ],
            },
        }),
        json.dumps({
            'type': 'user',
            'timestamp': '2026-07-11T10:53:14Z',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'SECRET_RESULT'}]},
        }),
        json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T10:54:06Z',
            'message': {
                'stop_reason': 'end_turn',
                'model': 'claude-opus-4-8[1m]',
                'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 500, 'cache_creation_input_tokens': 0},
                'content': [{'type': 'text', 'text': 'SECRET_TEXT'}],
            },
        }),
    ]


class TranscriptEnvTest(unittest.TestCase):
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

    def _write_transcript(self, session_id: str, cwd: str, lines: list[str]) -> None:
        path = transcript_path(session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines), encoding='utf-8')

    def _write_session(self, session_id: str, cwd: str, pid: int, name: str) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        sessions.mkdir(exist_ok=True)
        (sessions / f'{pid}.json').write_text(
            json.dumps({'pid': pid, 'sessionId': session_id, 'cwd': cwd, 'name': name, 'kind': 'interactive'}),
            encoding='utf-8',
        )


class ParseTest(TranscriptEnvTest):
    def test_extracts_control_metadata(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(_SESSION_ID, _CWD)

        self.assertTrue(state.has_transcript)
        self.assertEqual(state.last_stop_reason, 'end_turn')
        self.assertEqual(state.last_tool_name, 'Bash')
        self.assertFalse(state.pending_tool)

    def test_extracts_model_and_usage(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.model, 'claude-opus-4-8[1m]')
        self.assertEqual(state.usage['input_tokens'], 110)
        self.assertEqual(state.usage['output_tokens'], 55)
        self.assertEqual(state.usage['cache_read_input_tokens'], 1500)
        self.assertEqual(state.usage['cache_creation_input_tokens'], 200)

    def test_splits_cache_creation_by_ttl(self) -> None:
        lines = [
            json.dumps({
                'type': 'assistant',
                'timestamp': '2026-07-11T10:53:07Z',
                'message': {
                    'stop_reason': 'end_turn',
                    'usage': {
                        'input_tokens': 2, 'output_tokens': 123, 'cache_creation_input_tokens': 33000,
                        'cache_creation': {'ephemeral_5m_input_tokens': 1000, 'ephemeral_1h_input_tokens': 32000},
                    },
                    'content': [],
                },
            }),
            json.dumps({
                'type': 'assistant',
                'timestamp': '2026-07-11T10:54:07Z',
                'message': {
                    'stop_reason': 'end_turn',
                    'usage': {
                        'input_tokens': 3, 'output_tokens': 7, 'cache_creation_input_tokens': 500,
                        'cache_creation': {'ephemeral_5m_input_tokens': 500, 'ephemeral_1h_input_tokens': 0},
                    },
                    'content': [],
                },
            }),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.usage['cache_creation_5m_input_tokens'], 1500)
        self.assertEqual(state.usage['cache_creation_1h_input_tokens'], 32000)
        self.assertEqual(state.usage['cache_creation_input_tokens'], 33500)

    def test_tracks_usage_per_model(self) -> None:
        lines = [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T10:53:07Z',
                'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                            'usage': {'input_tokens': 100, 'output_tokens': 50}, 'content': []},
            }),
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T10:53:20Z', 'isSidechain': True,
                'message': {'stop_reason': 'end_turn', 'model': 'claude-haiku-4-5',
                            'usage': {'input_tokens': 8, 'output_tokens': 4}, 'content': []},
            }),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        # Overall totals sum every turn (subagents included).
        self.assertEqual(state.usage['input_tokens'], 108)
        # ...but the per-model split keeps the cheaper subagent model apart.
        self.assertEqual(state.usage_by_model['claude-opus-4-8']['input_tokens'], 100)
        self.assertEqual(state.usage_by_model['claude-haiku-4-5']['input_tokens'], 8)
        self.assertEqual(state.usage_by_model['claude-haiku-4-5']['output_tokens'], 4)

    def test_model_timeline_is_ordered_switch_log(self) -> None:
        def assistant(model: str, ts: str, sidechain: bool = False) -> str:
            entry = {'type': 'assistant', 'timestamp': ts,
                     'message': {'stop_reason': 'end_turn', 'model': model,
                                 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'content': []}}
            if sidechain:
                entry['isSidechain'] = True
            return json.dumps(entry)

        # Written out of order on disk (the scanner sorts by timestamp), with a
        # sidechain turn to exclude and a same-model run to collapse. The model
        # is used, left, and returned to - the switch back must appear as its own
        # trailing entry, not fold into the model's first appearance.
        lines = [
            assistant('claude-opus-4-8', '2026-07-11T09:00:00Z'),
            assistant('claude-sonnet-5', '2026-07-11T14:00:00Z'),
            assistant('claude-fable-5', '2026-07-11T11:00:00Z'),
            assistant('claude-haiku-4-5', '2026-07-11T10:30:00Z', sidechain=True),  # subagent - excluded
            assistant('claude-opus-4-8', '2026-07-11T16:00:00Z'),                   # switched back
            assistant('claude-opus-4-8', '2026-07-11T09:30:00Z'),                   # same run - collapses
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.model_timeline, [
            {'time': '2026-07-11T09:00:00Z', 'model': 'claude-opus-4-8'},
            {'time': '2026-07-11T11:00:00Z', 'model': 'claude-fable-5'},
            {'time': '2026-07-11T14:00:00Z', 'model': 'claude-sonnet-5'},
            {'time': '2026-07-11T16:00:00Z', 'model': 'claude-opus-4-8'},
        ])

    def test_synthetic_model_is_excluded_from_split_and_history(self) -> None:
        lines = [
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                        'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                                    'usage': {'input_tokens': 10, 'output_tokens': 20}, 'content': []}}),
            # A locally-generated (synthetic) turn: not a real model, zero usage.
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:05:00Z',
                        'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                                    'usage': {'input_tokens': 0, 'output_tokens': 0}, 'content': []}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        timeline_models = [entry['model'] for entry in state.model_timeline]
        # The synthetic sentinel must not appear as a model anywhere...
        self.assertNotIn('<synthetic>', state.usage_by_model)
        self.assertNotIn('<synthetic>', timeline_models)
        # ...while the real model is still tracked and the overall totals are intact.
        self.assertIn('claude-opus-4-8', state.usage_by_model)
        self.assertIn('claude-opus-4-8', timeline_models)
        self.assertEqual(state.usage['input_tokens'], 10)
        # The column shows the last real model, not the synthetic sentinel that followed it.
        self.assertEqual(state.model, 'claude-opus-4-8')

    def test_usage_accumulates_incrementally(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        first = state_for(_SESSION_ID, _CWD)

        extra = json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T10:55:00Z',
            'message': {'stop_reason': 'end_turn', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'content': []},
        })
        path = transcript_path(_SESSION_ID, _CWD)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('\n' + extra)

        second = state_for(_SESSION_ID, _CWD)

        self.assertEqual(second.usage['input_tokens'], first.usage['input_tokens'] + 7)
        self.assertEqual(second.usage['output_tokens'], first.usage['output_tokens'] + 3)

    def test_title_falls_back_to_first_prompt(self) -> None:
        caveat = '<local-command-caveat>Caveat: generated while running local commands.</local-command-caveat>'
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': caveat}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'content': '/commit'}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:02Z', 'message': {'content': 'later message'}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, '/commit')

    def test_slash_command_title_shows_command_name(self) -> None:
        caveat = '<local-command-caveat>Caveat.</local-command-caveat>'
        command = '<command-message>wm-tipps</command-message>\n<command-name>/wm-tipps</command-name>'
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': caveat}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'content': command}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, '/wm-tipps')

    def test_first_prompt_strips_ide_wrapper_blocks(self) -> None:
        content = '<ide_opened_file>The user opened a file.</ide_opened_file>Was bedeutet das Panel?'
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': content}}),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Was bedeutet das Panel?')

    def test_ai_title_outranks_first_prompt(self) -> None:
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': 'first prompt'}}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Generated title', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Generated title')

    def test_first_prompt_title_is_truncated(self) -> None:
        long_prompt = 'x' * 300
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': long_prompt}}),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(len(state.title), 80)
        self.assertTrue(state.title.endswith('…'))

    def test_custom_title_outranks_later_ai_title(self) -> None:
        lines = [
            json.dumps({'type': 'ai-title', 'aiTitle': 'Auto title', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'custom-title', 'customTitle': 'Manual title', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Newer auto title', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Manual title')

    def test_interrupt_marker_is_its_own_entry_kind(self) -> None:
        # Claude Code writes a fixed marker as a user turn when the user stops a
        # running turn. On disk it is a plain user turn, but it must be recognized
        # as an interrupt (control back with the user), not read as a fresh prompt.
        lines = [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T10:53:07Z',
                'message': {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 't9', 'name': 'Task', 'input': {}}]},
            }),
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T10:53:08Z',
                'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't9', 'content': 'x'}]},
            }),
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T10:53:09Z',
                'message': {'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]},
            }),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertFalse(state.pending_tool)
        self.assertEqual(state.last_entry_kind, 'user_interrupt')

    def test_plain_trailing_user_text_is_not_an_interrupt(self) -> None:
        # A normal trailing user message (a fresh prompt the model is now
        # thinking about) stays user_text, so it still reads as working.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T10:53:09Z',
                'message': {'content': [{'type': 'text', 'text': 'do the thing'}]},
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'user_text')

    def test_trailing_usage_limit_is_its_own_entry_kind(self) -> None:
        # A usage/session limit is written as a locally-generated (synthetic)
        # assistant turn with an error flag and a non-end_turn stop_reason. It
        # must read as its own api_error kind (never a pending assistant turn),
        # flagged as a usage limit, without leaking the message text.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': "You've hit your session limit"}]},
                'error': 'rate_limit', 'isApiErrorMessage': True, 'apiErrorStatus': 429,
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertTrue(state.usage_limited)

    def test_usage_limit_detected_from_error_token_without_a_status(self) -> None:
        # Defensive: a rate-limit turn whose status field is absent is still
        # recognized from the `error` token alone.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0}, 'content': []},
                'error': 'rate_limit', 'isApiErrorMessage': True,
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertTrue(state.usage_limited)

    def test_non_limit_api_error_is_not_flagged_usage_limited(self) -> None:
        # Other trailing API errors (an overload, a server error) are still the
        # api_error kind, but must not be labelled a usage limit.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': 'API Error: 529 overloaded'}]},
                'error': 'overloaded_error', 'isApiErrorMessage': True, 'apiErrorStatus': 529,
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertFalse(state.usage_limited)

    def test_api_error_superseded_by_a_later_real_turn(self) -> None:
        # A mid-conversation error that Claude Code retried and followed with a
        # real turn must not stick: the newest entry wins, so the kind is the
        # later assistant turn, not api_error.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0}, 'content': []},
                'error': 'overloaded_error', 'isApiErrorMessage': True, 'apiErrorStatus': 529,
            }),
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:40Z',
                'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                            'usage': {'input_tokens': 12, 'output_tokens': 8}, 'content': [{'type': 'text', 'text': 'ok'}]},
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertFalse(state.usage_limited)
        self.assertEqual(state.model, 'claude-opus-4-8')

    def test_extracts_latest_permission_mode(self) -> None:
        lines = [
            json.dumps({'type': 'permission-mode', 'permissionMode': 'default', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]}}),
            json.dumps({'type': 'permission-mode', 'permissionMode': 'auto', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'auto')

    def test_extracts_session_title(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Session title label')

    def test_title_beyond_tail_window_is_found(self) -> None:
        # Title generated once at the very beginning, then megabytes of later
        # entries - the tail parser cannot see it, the deep scan must.
        filler_entry = json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T11:00:00Z',
            'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x' * 120}]},
        })
        lines = [json.dumps({'type': 'ai-title', 'aiTitle': 'Early deep title', 'sessionId': _SESSION_ID})]
        lines.extend([filler_entry] * 2000)

        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Early deep title')

    def test_unresolved_tool_is_pending(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines()[:2])
        state = state_for(_SESSION_ID, _CWD)

        self.assertTrue(state.pending_tool)
        self.assertEqual(state.last_stop_reason, 'tool_use')

    def test_tail_splits_only_on_newline_keeping_unicode_lines_intact(self) -> None:
        # A JSON value may legitimately contain U+0085 (NEL), which str.splitlines
        # treats as a line boundary but the JSONL format does not. The tail parser
        # must split on '\n' only - otherwise the newest entry is shredded into
        # unparseable fragments and the state falls back to the prior turn.
        older = json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                            'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]}})
        newest = json.dumps({
            'type': 'assistant', 'timestamp': '2026-07-11T10:01:00Z',
            'message': {'stop_reason': 'tool_use',
                        'content': [{'type': 'text', 'text': 'before\u0085after'},
                                    {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}}]},
        }, ensure_ascii=False)
        self._write_transcript(_SESSION_ID, _CWD, [older, newest])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_stop_reason, 'tool_use')
        self.assertEqual(state.last_tool_name, 'Bash')
        self.assertTrue(state.pending_tool)

    def test_tail_window_escalates_past_giant_entries(self) -> None:
        # A single entry larger than the tail window would otherwise leave
        # nothing parseable and the state blind.
        giant = json.dumps({
            'type': 'user', 'timestamp': '2026-07-11T11:00:01Z',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tg', 'content': 'y' * 400000}]},
        })
        lines = [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T11:00:00Z',
                'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]},
            }),
            giant,
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertIsNotNone(state.last_timestamp)
        self.assertEqual(state.last_entry_kind, 'tool_result')
        self.assertEqual(state.last_stop_reason, 'end_turn')

    def test_missing_transcript(self) -> None:
        state = state_for('no-such-id', _CWD)
        self.assertFalse(state.has_transcript)


class ActivityAgeTest(TranscriptEnvTest):
    def test_age_uses_last_entry_timestamp_not_file_mtime(self) -> None:
        # The last real turn is five minutes old; an idle process then bumps
        # the file mtime to "now" without appending anything.  The age must
        # follow the entry timestamp, not the mtime.
        now = time.time()
        recorded = datetime.fromtimestamp(now - 300, tz=timezone.utc).replace(microsecond=0)
        stamp = recorded.strftime('%Y-%m-%dT%H:%M:%SZ')
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'assistant', 'timestamp': stamp,
                        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]}}),
        ])
        os.utime(transcript_path(_SESSION_ID, _CWD), (now, now))

        state = state_for(_SESSION_ID, _CWD)

        self.assertIsNotNone(state.age_seconds)
        self.assertGreater(state.age_seconds, 250)
        self.assertLess(state.age_seconds, 400)

    def test_age_falls_back_to_file_mtime_without_timestamps(self) -> None:
        # A transcript with only metadata entries carries no timestamp, so the
        # age has nothing to derive from and falls back to the file mtime.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'ai-title', 'aiTitle': 'A title', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'custom-title', 'customTitle': 'B title', 'sessionId': _SESSION_ID}),
        ])
        now = time.time()
        os.utime(transcript_path(_SESSION_ID, _CWD), (now - 100, now - 100))

        state = state_for(_SESSION_ID, _CWD)

        self.assertIsNone(state.last_timestamp)
        self.assertIsNotNone(state.age_seconds)
        self.assertGreater(state.age_seconds, 60)
        self.assertLess(state.age_seconds, 200)


class PrivacyTest(TranscriptEnvTest):
    def test_parsed_state_leaks_no_content(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(_SESSION_ID, _CWD)

        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_api_error_message_text_is_never_read(self) -> None:
        # A usage-limit / API-error turn carries a human-readable message (which
        # can quote arbitrary context); only the structural error fields may be
        # read, never that text.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': 'SECRET_TEXT hit your session limit'}]},
                'error': 'rate_limit', 'isApiErrorMessage': True, 'apiErrorStatus': 429,
            }),
        ])
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertTrue(state.usage_limited)
        self.assertNotIn('SECRET_TEXT', json.dumps(asdict(state)))

    def test_only_first_prompt_is_read_never_later_messages(self) -> None:
        # The first prompt is the sanctioned display title; every later user
        # message must stay unread.
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': 'Benign question'}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'content': 'SECRET_LATER_MESSAGE'}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(_SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Benign question')
        self.assertNotIn('SECRET_LATER_MESSAGE', json.dumps(asdict(state)))

    def test_history_state_reads_cwd_but_no_content(self) -> None:
        # The history scan reads a session's cwd (a path, not conversation
        # content) so a past session can be grouped under its project - but it
        # must still leave every content field unread, exactly like state_for.
        lines = [
            json.dumps({'type': 'ai-title', 'aiTitle': 'History title', 'sessionId': _SESSION_ID}),
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T10:53:07Z', 'cwd': _CWD,
                'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                            'usage': {'input_tokens': 1, 'output_tokens': 1},
                            'content': [{'type': 'text', 'text': 'SECRET_TEXT'}]},
            }),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = history_state_for(transcript_path(_SESSION_ID, _CWD))

        self.assertEqual(state.title, 'History title')
        self.assertEqual(state.cwd, _CWD)
        self.assertEqual(state.model, 'claude-opus-4-8')
        for secret in _SECRETS:
            self.assertNotIn(secret, json.dumps(asdict(state)))

    def test_snapshot_leaks_no_content(self) -> None:
        # Use this test process's own PID so the session counts as alive and
        # is therefore included in the snapshot rather than filtered out.
        pid = os.getpid()
        self._write_session(_SESSION_ID, _CWD, pid, 'privacy-name')
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())

        serialized = json.dumps(build_snapshot())

        self.assertIn('privacy-name', serialized)
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)


if __name__ == '__main__':
    unittest.main()
