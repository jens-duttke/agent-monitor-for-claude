"""Tests for the transcript tail parser (_parse control-flow classification)."""
from __future__ import annotations

import json
import unittest

from agent_monitor_for_claude.transcript import _parse


def _lines(*entries: dict) -> list[str]:
    return [json.dumps(entry) for entry in entries]


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


if __name__ == '__main__':
    unittest.main()
