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


def _turn(stop_reason: str) -> str:
    return json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'stop_reason': stop_reason, 'content': []}})


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

    def _add_agent(self, name: str, age_seconds: float, description: str,
                   finished: bool = False, nested: bool = False, body: str | None = None) -> None:
        directory = self._dir / 'workflows' / 'wf_1' if nested else self._dir
        directory.mkdir(parents=True, exist_ok=True)

        agent = directory / f'agent-{name}.jsonl'
        agent.write_text(body if body is not None else _turn('end_turn' if finished else 'tool_use'), encoding='utf-8')
        (directory / f'agent-{name}.meta.json').write_text(
            json.dumps({'agentType': 'general-purpose', 'description': description}), encoding='utf-8')

        mtime = time.time() - age_seconds
        os.utime(agent, (mtime, mtime))

    def test_no_directory_is_empty(self) -> None:
        self.assertEqual(count_subagents('other-id', _CWD).running, 0)

    def test_running_is_not_finished_within_window(self) -> None:
        self._add_agent('a', 2, 'Task A')                       # running
        self._add_agent('b', 400, 'Task B')                     # still going, no end_turn -> running
        self._add_agent('c', 120, 'Task C', finished=True)      # finished -> recent_done
        self._add_agent('d', 99999, 'Task D')                   # older than window -> ignored

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 2)
        self.assertEqual(info.recent_done, 1)
        self.assertEqual(set(info.labels), {'Task A', 'Task B'})

    def test_finds_nested_workflow_agents(self) -> None:
        self._add_agent('w', 3, 'Workflow agent', nested=True)

        info = count_subagents(_SESSION_ID, _CWD)

        self.assertEqual(info.running, 1)
        self.assertEqual(info.labels, ('Workflow agent',))

    def test_never_surfaces_subagent_transcript_content(self) -> None:
        self._add_agent('s', 2, 'Benign label', body='{"stop_reason":"tool_use"} SECRET_SUBAGENT_BODY')

        info = count_subagents(_SESSION_ID, _CWD)

        serialized = json.dumps(info.labels)
        self.assertIn('Benign label', serialized)
        self.assertNotIn('SECRET_SUBAGENT_BODY', serialized)


if __name__ == '__main__':
    unittest.main()
