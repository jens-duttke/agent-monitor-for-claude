"""Tests for the transcript tail parser (_parse control-flow classification)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_monitor_for_claude.transcript import _absorb_line, _model_timeline, _parse, _scan_title_cwd, _ScanState


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


class ModelTimelineOrderTest(unittest.TestCase):
    def test_sorts_chronologically_not_lexicographically(self) -> None:
        # '...07.500Z' is chronologically LATER than '...07Z' but sorts BEFORE it
        # as a raw string ('.' < 'Z'), so a lexicographic sort would name the
        # wrong model as current.
        timeline = _model_timeline([
            ('2026-07-11T10:53:07Z', 'opus'),
            ('2026-07-11T10:53:07.500Z', 'sonnet'),
        ])
        self.assertEqual([entry['model'] for entry in timeline], ['opus', 'sonnet'])
        self.assertEqual(timeline[-1]['time'], '2026-07-11T10:53:07.500Z')

    def test_unparseable_timestamps_do_not_crash(self) -> None:
        timeline = _model_timeline([('not-a-timestamp', 'opus'), ('2026-07-11T10:00:00Z', 'sonnet')])
        self.assertEqual({entry['model'] for entry in timeline}, {'opus', 'sonnet'})


if __name__ == '__main__':
    unittest.main()
