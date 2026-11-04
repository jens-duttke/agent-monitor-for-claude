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
from unittest import mock

from agent_monitor_for_claude import background_scans, transcript
from agent_monitor_for_claude.paths import transcript_path, local_root
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
            'version': '2.1.224',
            'entrypoint': 'claude-vscode',
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
            'version': '2.1.224',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'SECRET_RESULT'}]},
        }),
        json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T10:54:06Z',
            'version': '2.1.226',
            'entrypoint': 'cli',
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
        path = transcript_path(local_root(), session_id, cwd)
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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertTrue(state.has_transcript)
        self.assertEqual(state.last_stop_reason, 'end_turn')
        self.assertEqual(state.last_tool_name, 'Bash')
        self.assertFalse(state.pending_tool)

    def test_extracts_model_and_usage(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        # Overall totals sum every turn (subagents included).
        self.assertEqual(state.usage['input_tokens'], 108)
        # ...but the per-model split keeps the cheaper subagent model apart.
        self.assertEqual(state.usage_by_model['claude-opus-4-8']['input_tokens'], 100)
        self.assertEqual(state.usage_by_model['claude-haiku-4-5']['input_tokens'], 8)
        self.assertEqual(state.usage_by_model['claude-haiku-4-5']['output_tokens'], 4)

    def test_one_response_split_over_entries_counts_its_usage_once(self) -> None:
        # Claude Code writes one API response as one entry per content block, and
        # every entry repeats the response's full usage. Summing each entry
        # counted a session's tokens and cost about twice over.
        def block(message_id: str | None, block_type: str, timestamp: str, model: str = 'claude-opus-4-8') -> str:
            message = {'stop_reason': 'tool_use', 'model': model, 'content': [{'type': block_type}],
                       'usage': {'input_tokens': 10, 'output_tokens': 40, 'cache_read_input_tokens': 1000,
                                 'cache_creation_input_tokens': 300,
                                 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 300}}}
            if message_id is not None:
                message['id'] = message_id
            return json.dumps({'type': 'assistant', 'timestamp': timestamp, 'message': message})

        lines = [
            block('msg_a', 'thinking', '2026-07-11T10:00:00Z'),
            block('msg_a', 'text', '2026-07-11T10:00:01Z'),
            block('msg_a', 'tool_use', '2026-07-11T10:00:02Z'),
            block('msg_b', 'text', '2026-07-11T10:01:00Z', model='claude-haiku-4-5'),
            block('msg_b', 'tool_use', '2026-07-11T10:01:01Z', model='claude-haiku-4-5'),
            # No id to match on: counted on its own, never merged into another response.
            block(None, 'text', '2026-07-11T10:02:00Z'),
            block(None, 'text', '2026-07-11T10:02:01Z'),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.usage['output_tokens'], 4 * 40)
        self.assertEqual(state.usage['cache_read_input_tokens'], 4 * 1000)
        self.assertEqual(state.usage['cache_creation_1h_input_tokens'], 4 * 300)
        self.assertEqual(state.usage_by_model['claude-opus-4-8']['output_tokens'], 3 * 40)
        self.assertEqual(state.usage_by_model['claude-haiku-4-5']['output_tokens'], 40)

    def test_a_mid_stream_entry_counts_only_up_to_the_final_output(self) -> None:
        # An early entry of a response can carry the output count reached so far;
        # a later entry of the same response raises it. The input and cache
        # figures are identical in both and must still count once.
        def entry(output: int, timestamp: str) -> str:
            return json.dumps({'type': 'assistant', 'timestamp': timestamp,
                               'message': {'id': 'msg_a', 'stop_reason': 'tool_use', 'model': 'claude-opus-4-8', 'content': [],
                                           'usage': {'input_tokens': 5, 'output_tokens': output, 'cache_read_input_tokens': 900}}})

        self._write_transcript(_SESSION_ID, _CWD, [entry(12, '2026-07-11T10:00:00Z'), entry(80, '2026-07-11T10:00:05Z'),
                                                   entry(80, '2026-07-11T10:00:06Z')])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.usage['output_tokens'], 80)
        self.assertEqual(state.usage['input_tokens'], 5)
        self.assertEqual(state.usage['cache_read_input_tokens'], 900)
        self.assertEqual(state.usage_by_model['claude-opus-4-8']['output_tokens'], 80)

    def test_a_response_continued_after_an_incremental_scan_is_not_counted_again(self) -> None:
        # The entries of one response can straddle two polls; the second poll
        # must still recognize the response it already counted.
        entry = {'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                 'message': {'id': 'msg_a', 'stop_reason': 'tool_use', 'model': 'claude-opus-4-8', 'content': [],
                             'usage': {'input_tokens': 5, 'output_tokens': 50}}}
        self._write_transcript(_SESSION_ID, _CWD, [json.dumps(entry)])
        first = state_for(local_root(), _SESSION_ID, _CWD)

        with transcript_path(local_root(), _SESSION_ID, _CWD).open('a', encoding='utf-8') as handle:
            handle.write('\n' + json.dumps(entry))
        second = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(first.usage['output_tokens'], 50)
        self.assertEqual(second.usage['output_tokens'], 50)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.model_timeline, [
            {'time': '2026-07-11T09:00:00Z', 'model': 'claude-opus-4-8'},
            {'time': '2026-07-11T11:00:00Z', 'model': 'claude-fable-5'},
            {'time': '2026-07-11T14:00:00Z', 'model': 'claude-sonnet-5'},
            {'time': '2026-07-11T16:00:00Z', 'model': 'claude-opus-4-8'},
        ])

    def test_cli_version_is_metadata_read_end_to_end(self) -> None:
        # The Claude Code version and the entrypoint of the process that wrote
        # the turn are control metadata (which build wrote it, from where), not
        # conversation content: read deliberately, and - like the model - both
        # as the current value and as the run-compressed upgrade log.
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.cli_version, '2.1.226')
        self.assertEqual(state.cli_timeline, [
            {'time': '2026-07-11T10:53:07Z', 'version': '2.1.224', 'entrypoint': 'claude-vscode'},
            {'time': '2026-07-11T10:54:06Z', 'version': '2.1.226', 'entrypoint': 'cli'},
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
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        first = state_for(local_root(), _SESSION_ID, _CWD)

        extra = json.dumps({
            'type': 'assistant',
            'timestamp': '2026-07-11T10:55:00Z',
            'message': {'stop_reason': 'end_turn', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'content': []},
        })
        path = transcript_path(local_root(), _SESSION_ID, _CWD)
        with path.open('a', encoding='utf-8') as handle:
            handle.write('\n' + extra)

        second = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.title, '/commit')

    def test_slash_command_title_shows_command_name(self) -> None:
        caveat = '<local-command-caveat>Caveat.</local-command-caveat>'
        command = '<command-message>wm-tipps</command-message>\n<command-name>/wm-tipps</command-name>'
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': caveat}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'content': command}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.title, '/wm-tipps')

    def test_first_prompt_strips_ide_wrapper_blocks(self) -> None:
        content = '<ide_opened_file>The user opened a file.</ide_opened_file>Was bedeutet das Panel?'
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': content}}),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Was bedeutet das Panel?')

    def test_ai_title_outranks_first_prompt(self) -> None:
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': 'first prompt'}}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Generated title', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Generated title')

    def test_first_prompt_title_is_truncated(self) -> None:
        long_prompt = 'x' * 300
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': long_prompt}}),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(len(state.title), 80)
        self.assertTrue(state.title.endswith('…'))

    def test_custom_title_outranks_later_ai_title(self) -> None:
        lines = [
            json.dumps({'type': 'ai-title', 'aiTitle': 'Auto title', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'custom-title', 'customTitle': 'Manual title', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Newer auto title', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertFalse(state.pending_tool)
        self.assertEqual(state.last_entry_kind, 'user_interrupt')

    def test_local_command_output_is_a_kind_and_its_text_never_escapes(self) -> None:
        # The captured output of a local command is read only far enough to match
        # the fixed wrapper prefix. The command's actual output is conversation-
        # adjacent content and must not reach the parsed state or the snapshot,
        # exactly like the interrupt marker above.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T10:53:09Z',
                'message': {'role': 'user', 'content': '<local-command-stdout>SECRET_STDOUT</local-command-stdout>'},
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'local_command')
        self.assertNotIn('SECRET_STDOUT', json.dumps(asdict(state)))

    def test_plain_trailing_user_text_is_not_an_interrupt(self) -> None:
        # A normal trailing user message (a fresh prompt the model is now
        # thinking about) stays user_text, so it still reads as working.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T10:53:09Z',
                'message': {'content': [{'type': 'text', 'text': 'do the thing'}]},
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'user_text')

    def test_trailing_usage_limit_is_its_own_entry_kind(self) -> None:
        # A usage/session limit is written as a locally-generated (synthetic)
        # assistant turn with an error flag and a non-end_turn stop_reason. It
        # must read as its own api_error kind (never a pending assistant turn),
        # reporting the structural error fields, without leaking the message text.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': "You've hit your session limit"}]},
                'error': 'rate_limit', 'isApiErrorMessage': True, 'apiErrorStatus': 429,
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertEqual(state.api_error_kind, 'rate_limit')
        self.assertEqual(state.api_error_status, 429)

    def test_error_token_is_read_when_no_status_is_written(self) -> None:
        # Defensive: a rate-limit turn whose status field is absent must still
        # report the `error` token, which on its own names the cause.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0}, 'content': []},
                'error': 'rate_limit', 'isApiErrorMessage': True,
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertEqual(state.api_error_kind, 'rate_limit')
        self.assertIsNone(state.api_error_status)

    def test_non_limit_api_error_reports_its_own_fields(self) -> None:
        # Other trailing API errors (an overload, a server error) are the same
        # api_error kind and report their own token and status, which is what
        # lets the UI name a cause other than a usage limit.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': 'API Error: 529 overloaded'}]},
                'error': 'overloaded_error', 'isApiErrorMessage': True, 'apiErrorStatus': 529,
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertEqual(state.api_error_kind, 'overloaded_error')
        self.assertEqual(state.api_error_status, 529)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertIsNone(state.api_error_kind)
        self.assertIsNone(state.api_error_status)
        self.assertEqual(state.model, 'claude-opus-4-8')

    def test_extracts_latest_permission_mode(self) -> None:
        lines = [
            json.dumps({'type': 'permission-mode', 'permissionMode': 'default', 'sessionId': _SESSION_ID}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:00Z',
                        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]}}),
            json.dumps({'type': 'permission-mode', 'permissionMode': 'auto', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'auto')

    def test_permission_mode_from_entry_field(self) -> None:
        # A VS Code session writes no 'permission-mode' entry at all; the mode in
        # force is stamped onto each ordinary entry instead.
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'permissionMode': 'acceptEdits',
                        'message': {'content': [{'type': 'text', 'text': 'first'}]}}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:30Z',
                        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'permissionMode': 'auto',
                        'message': {'content': [{'type': 'text', 'text': 'second'}]}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'auto')

    def test_permission_mode_mixes_both_shapes(self) -> None:
        # A session continued in the CLI writes both shapes; whichever comes last
        # in the file is the current mode.
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'permissionMode': 'default',
                        'message': {'content': [{'type': 'text', 'text': 'prompt'}]}}),
            json.dumps({'type': 'permission-mode', 'permissionMode': 'plan', 'sessionId': _SESSION_ID}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'plan')

    def test_permission_mode_ignores_sidechain_entries(self) -> None:
        # A subagent turn does not report the main conversation's mode.
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'permissionMode': 'auto',
                        'message': {'content': [{'type': 'text', 'text': 'prompt'}]}}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T10:00:30Z', 'isSidechain': True,
                        'permissionMode': 'default',
                        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'auto')

    def test_permission_mode_ignores_malformed_value(self) -> None:
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:00:00Z', 'permissionMode': 'auto',
                        'message': {'content': [{'type': 'text', 'text': 'prompt'}]}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:01:00Z', 'permissionMode': 7,
                        'message': {'content': [{'type': 'text', 'text': 'next'}]}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:02:00Z', 'permissionMode': '',
                        'message': {'content': [{'type': 'text', 'text': 'more'}]}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'auto')

    def test_extracts_session_title(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.title, 'Early deep title')

    def test_unresolved_tool_is_pending(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines()[:2])
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertIsNotNone(state.last_timestamp)
        self.assertEqual(state.last_entry_kind, 'tool_result')
        self.assertEqual(state.last_stop_reason, 'end_turn')

    def test_missing_transcript(self) -> None:
        state = state_for(local_root(), 'no-such-id', _CWD)
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
        os.utime(transcript_path(local_root(), _SESSION_ID, _CWD), (now, now))

        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        os.utime(transcript_path(local_root(), _SESSION_ID, _CWD), (now - 100, now - 100))

        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertIsNone(state.last_timestamp)
        self.assertIsNotNone(state.age_seconds)
        self.assertGreater(state.age_seconds, 60)
        self.assertLess(state.age_seconds, 200)


class PrivacyTest(TranscriptEnvTest):
    def test_parsed_state_leaks_no_content(self) -> None:
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines())
        state = state_for(local_root(), _SESSION_ID, _CWD)

        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_pending_state_leaks_no_content(self) -> None:
        # While a large transcript's first scan runs in the background, the title
        # and mode are read from near the end of the file instead.  A prompt that
        # carries the mode passes that read's marker filter and is parsed, yet its
        # text must stay unread - it is not the session's first prompt.
        prompt = json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:55:00Z', 'permissionMode': 'default',
                             'message': {'content': 'SECRET_TEXT'}})
        self._write_transcript(_SESSION_ID, _CWD, _transcript_lines() + [prompt])
        try:
            with mock.patch.object(transcript, '_SYNC_SCAN_BYTES', 0):
                state = state_for(local_root(), _SESSION_ID, _CWD)
        finally:
            worker = background_scans._deferred.worker
            if worker is not None:
                worker.join(timeout=10)

        self.assertTrue(state.scan_pending)
        self.assertEqual(state.permission_mode, 'default')
        self.assertEqual(state.title, 'Session title label')
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_a_classifier_denial_is_counted_never_quoted(self) -> None:
        # The denial streak is read from the entry's own toolDenialKind field.
        # The refusal Claude Code writes alongside it is text - a long one - and
        # must stay unread: only the count crosses into the state.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'user', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                         'content': 'SECRET_RESULT', 'is_error': True}]},
                'toolUseResult': 'SECRET_TEXT',
                'toolDenialKind': 'automode-blocked',
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.auto_denials_consecutive, 1)
        self.assertEqual(state.auto_denials_total, 1)
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_only_the_error_message_first_line_is_read(self) -> None:
        # The sanctioned, bounded exception: an API-error turn's own message line
        # is read for the status tooltip, because it names what the structural
        # fields cannot (here the reset time). The bound is what is guarded - the
        # FIRST line of the FIRST text block, and nothing else in the entry: a
        # second line, a further block, and a thinking block all stay unread.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [
                                {'type': 'thinking', 'thinking': 'SECRET_THINKING'},
                                {'type': 'text', 'text': "You've hit your session limit\nSECRET_TEXT"},
                                {'type': 'text', 'text': 'SECRET_RESULT'},
                            ]},
                'error': 'rate_limit', 'isApiErrorMessage': True, 'apiErrorStatus': 429,
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'api_error')
        self.assertEqual(state.api_error_kind, 'rate_limit')
        self.assertEqual(state.api_error_detail, "You've hit your session limit")
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_an_ordinary_turn_text_is_never_read_as_an_error_detail(self) -> None:
        # The read above is keyed on the isApiErrorMessage flag alone. An
        # ordinary assistant turn - the same shape, minus that flag - must yield
        # no detail at all, or the exception would quietly cover every turn.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({
                'type': 'assistant', 'timestamp': '2026-07-11T13:19:14Z',
                'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                            'content': [{'type': 'text', 'text': 'SECRET_TEXT'}]},
            }),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.last_entry_kind, 'assistant')
        self.assertIsNone(state.api_error_detail)
        self.assertNotIn('SECRET_TEXT', json.dumps(asdict(state)))

    def test_the_turn_refinements_read_flags_never_text(self) -> None:
        # How a turn ended, when a loop wakes, what opened the turn: each is read
        # from a control field beside text that must stay unread - the wrap-up's
        # recap, question and actions, the wakeup's prompt and reason, and the
        # body and sender name of a message from another session.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T13:19:00Z', 'isMeta': True,
                        'origin': {'kind': 'peer', 'from': 'SECRET_INPUT', 'name': 'SECRET_INPUT', 'body': 'SECRET_TEXT'},
                        'message': {'content': 'SECRET_TEXT'}}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T13:19:05Z',
                        'message': {'stop_reason': 'tool_use', 'model': 'claude-opus-5-5', 'content': [
                            {'type': 'tool_use', 'id': 't1', 'name': 'ScheduleWakeup',
                             'input': {'delaySeconds': 300, 'reason': 'SECRET_INPUT', 'prompt': 'SECRET_INPUT'}},
                            {'type': 'tool_use', 'id': 't2', 'name': 'mcp__ccd_turn__wrap_up',
                             'input': {'recap': 'SECRET_INPUT', 'ask': 'SECRET_INPUT', 'actions': ['SECRET_INPUT'], 'blocked': True}},
                        ]}}),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.turn_origin, 'peer')
        self.assertEqual(state.turn_end_signal, 'blocked')
        self.assertIsNotNone(state.wakeup_at)
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_the_session_details_read_names_and_numbers_never_text(self) -> None:
        # Each detail is a control value beside text that must stay unread: the
        # plan's step texts, a failing hook's output and command, a queued
        # prompt, an artifact's title and address, and what a file-writing tool
        # wrote. The file a tool wrote to is counted, never reported.
        self._write_transcript(_SESSION_ID, _CWD, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T13:19:00Z', 'message': {'content': 'Benign question'}}),
            json.dumps({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': '2026-07-11T13:19:01Z',
                        'content': 'SECRET_TEXT'}),
            json.dumps({'type': 'attachment', 'timestamp': '2026-07-11T13:19:02Z', 'gitBranch': 'feature/x',
                        'attachment': {'type': 'hook_non_blocking_error', 'hookName': 'PostToolUse:Bash', 'exitCode': 7,
                                       'stderr': 'SECRET_RESULT', 'stdout': 'SECRET_RESULT', 'command': 'SECRET_INPUT'}}),
            json.dumps({'type': 'frame-link', 'artifactCount': 2, 'title': 'SECRET_TEXT', 'frameUrl': 'https://SECRET_INPUT'}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T13:19:05Z', 'effort': 'high',
                        'message': {'id': 'm1', 'stop_reason': 'tool_use', 'model': 'claude-opus-5-5',
                                    'usage': {'input_tokens': 10, 'cache_read_input_tokens': 1000, 'output_tokens': 5},
                                    'content': [
                                        {'type': 'tool_use', 'id': 't1', 'name': 'mcp__ccd_turn__update_plan',
                                         'input': {'scope': 'SECRET_INPUT', 'items': [
                                             {'content': 'SECRET_INPUT', 'status': 'in_progress'},
                                             {'content': 'SECRET_INPUT', 'status': 'pending'}]}},
                                        {'type': 'tool_use', 'id': 't2', 'name': 'Edit',
                                         'input': {'file_path': 'SECRET_INPUT_PATH', 'old_string': 'SECRET_INPUT',
                                                   'new_string': 'SECRET_INPUT'}},
                                    ]}}),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual((state.plan_step, state.plan_total), (1, 2))
        self.assertEqual(state.hook_errors[0].hook, 'PostToolUse:Bash')
        self.assertEqual(state.queued, 1)
        self.assertEqual(state.artifact_count, 2)
        self.assertEqual(state.files_edited, 1)
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_only_the_retry_summary_first_line_is_read(self) -> None:
        # A retried request's one sanctioned line is the CLI's own short summary,
        # first line only: a second line and the raw error message stay unread.
        self._write_transcript(_SESSION_ID, _CWD, [
            # The first prompt is the sanctioned title, so it carries no marker.
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T13:19:00Z', 'message': {'content': 'Benign question'}}),
            json.dumps({'type': 'system', 'subtype': 'api_error', 'timestamp': '2026-07-11T13:19:05Z',
                        'retryAttempt': 1, 'maxRetries': 10,
                        'error': {'formatted': 'Connection dropped (ECONNRESET)\nSECRET_RESULT', 'message': 'SECRET_INPUT',
                                  'connection': {'message': 'SECRET_INPUT'}}}),
        ])
        state = state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.api_retry.detail, 'Connection dropped (ECONNRESET)')
        serialized = json.dumps(asdict(state))
        for secret in _SECRETS:
            self.assertNotIn(secret, serialized)

    def test_only_first_prompt_is_read_never_later_messages(self) -> None:
        # The first prompt is the sanctioned display title; every later user
        # message must stay unread.
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': 'Benign question'}}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:01Z', 'message': {'content': 'SECRET_LATER_MESSAGE'}}),
        ]
        self._write_transcript(_SESSION_ID, _CWD, lines)
        state = state_for(local_root(), _SESSION_ID, _CWD)

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
        state = history_state_for(transcript_path(local_root(), _SESSION_ID, _CWD))

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
