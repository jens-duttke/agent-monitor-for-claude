"""Tests for subagent counting and its privacy boundary."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_monitor_for_claude.paths import cwd_to_slug, projects_dir
from agent_monitor_for_claude.subagents import count_subagents

_SESSION_ID = 'sub-session-id'
_CWD = 'd:\\WebDev\\proj'


def _line(obj: dict) -> str:
    return json.dumps(obj)


def _assistant_tool_use(name: str = 'Read', tool_id: str = 'tu_1', extra_input: dict | None = None) -> str:
    """An assistant turn that ends on a tool call (stop_reason tool_use)."""
    block: dict = {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': extra_input or {}}
    return _line({'type': 'assistant', 'isSidechain': True, 'message': {'stop_reason': 'tool_use', 'content': [block]}})


def _tool_result(tool_id: str = 'tu_1') -> str:
    """A user turn answering a tool call."""
    return _line({'type': 'user', 'isSidechain': True, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_id}]}})


def _assistant_end_turn() -> str:
    """An assistant turn that ended naturally."""
    return _line({'type': 'assistant', 'isSidechain': True, 'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'done'}]}})


# A completed workflow agent: its final act is a StructuredOutput tool call whose
# tool_result is the last entry - there is no trailing end_turn.
_WORKFLOW_DONE_BODY = _assistant_tool_use('StructuredOutput', 'tu_9') + '\n' + _tool_result('tu_9')


class SubagentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        self._dir = projects_dir() / cwd_to_slug(_CWD) / _SESSION_ID / 'subagents'
        self._dir.mkdir(parents=True)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _add_agent(self, name: str, age_seconds: float, description: str, body: str, nested: bool = False) -> None:
        directory = self._dir / 'workflows' / 'wf_1' if nested else self._dir
        directory.mkdir(parents=True, exist_ok=True)

        agent = directory / f'agent-{name}.jsonl'
        agent.write_text(body, encoding='utf-8')
        (directory / f'agent-{name}.meta.json').write_text(
            json.dumps({'agentType': 'general-purpose', 'description': description}), encoding='utf-8')

        mtime = time.time() - age_seconds
        os.utime(agent, (mtime, mtime))

    def test_no_directory_is_empty(self) -> None:
        self.assertEqual(count_subagents('other-id', _CWD).running, 0)

    def test_executing_tool_is_running(self) -> None:
        # Last entry is an unanswered tool_use -> executing -> running, however
        # long the tool has been going (freshness alone must not drop it).
        self._add_agent('a', 5, 'Task A', body=_assistant_tool_use())
        self._add_agent('b', 400, 'Task B', body=_assistant_tool_use())

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 2)
        self.assertEqual(info.recent_done, 0)
        self.assertEqual(set(info.labels), {'Task A', 'Task B'})

    def test_end_turn_is_finished(self) -> None:
        self._add_agent('c', 5, 'Task C', body=_assistant_end_turn())

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 1)

    def test_settled_workflow_completion_is_finished(self) -> None:
        # The reported bug: a workflow agent finishes on a resolved StructuredOutput
        # tool call (no trailing end_turn) and, once quiet, must read as finished -
        # not stuck as running until the recent window expires.
        self._add_agent('wf', 120, 'WF done', body=_WORKFLOW_DONE_BODY)

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 1)

    def test_fresh_answered_turn_is_still_running(self) -> None:
        # Same shape as a completed workflow agent, but written seconds ago: the
        # agent is between a tool_result and its next turn, so it must not be
        # misread as done (no flicker while it is still working).
        self._add_agent('think', 3, 'Thinking', body=_WORKFLOW_DONE_BODY)

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 1)
        self.assertEqual(info.labels, ('Thinking',))

    def test_finds_nested_workflow_agents(self) -> None:
        self._add_agent('w', 3, 'Workflow agent', body=_assistant_tool_use(), nested=True)

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 1)
        self.assertEqual(info.labels, ('Workflow agent',))

    def test_older_than_window_is_ignored(self) -> None:
        self._add_agent('old', 99999, 'Task Old', body=_assistant_tool_use())

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 0)

    def test_never_surfaces_subagent_transcript_content(self) -> None:
        body = _assistant_tool_use('Read', extra_input={'note': 'SECRET_SUBAGENT_BODY'})
        self._add_agent('s', 2, 'Benign label', body=body)

        info = count_subagents(_SESSION_ID, _CWD)

        serialized = json.dumps(info.labels)
        self.assertIn('Benign label', serialized)
        self.assertNotIn('SECRET_SUBAGENT_BODY', serialized)


if __name__ == '__main__':
    unittest.main()
