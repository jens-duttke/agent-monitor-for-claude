"""Tests for subagent counting and its privacy boundary."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import subagents as smod
from agent_monitor_for_claude.paths import cwd_to_slug, projects_dir, local_root
from agent_monitor_for_claude.subagents import agent_texts, count_subagents


def _task(root, session_id: str, agent_id: str) -> str | None:
    """The task :func:`agent_texts` reads for an agent, or None when it finds the agent without one or not at all."""
    texts = agent_texts(root, session_id, agent_id)
    return (texts['task'] or None) if texts is not None else None

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
        self._dir = projects_dir(local_root()) / cwd_to_slug(_CWD) / _SESSION_ID / 'subagents'
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

    def test_an_agents_whole_task_is_read_from_its_first_line(self) -> None:
        task = 'Map the input handling.\n' + 'x' * 5000
        first = json.dumps({'type': 'user', 'isSidechain': True, 'message': {'content': task}})
        self._add_agent('a1b2c3d4e5f6a7b8c', 5, 'Map', body=first + '\n' + _assistant_end_turn())
        # An agent filed under a worktree's project folder, its task in text blocks.
        worktree_dir = projects_dir(local_root()) / cwd_to_slug('d:\\WebDev\\proj-worktrees\\fix') / _SESSION_ID / 'subagents'
        worktree_dir.mkdir(parents=True)
        blocks = {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'Part one'}, {'type': 'image'}, {'type': 'text', 'text': 'Part two'}]}}
        (worktree_dir / 'agent-aw1.jsonl').write_text(json.dumps(blocks) + '\n', encoding='utf-8')

        self.assertEqual(_task(local_root(), _SESSION_ID, 'a1b2c3d4e5f6a7b8c'), task)
        self.assertEqual(_task(local_root(), _SESSION_ID, 'aw1'), 'Part one\n\nPart two')
        self.assertIsNone(_task(local_root(), _SESSION_ID, 'missing'))
        self.assertIsNone(_task(local_root(), 'other-session', 'a1b2c3d4e5f6a7b8c'))

    def test_an_agents_result_is_its_newest_reply_whole(self) -> None:
        first = json.dumps({'type': 'user', 'isSidechain': True, 'message': {'content': 'The task'}})
        report = 'Findings:\n' + 'z' * 5000
        lines = [
            first,
            json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'Looking.'}]}}),
            json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [
                {'type': 'thinking', 'thinking': 'never shown'}, {'type': 'text', 'text': report}]}}),
            json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'tool_use', 'id': 'u', 'name': 'Read', 'input': {}}]}}),
            json.dumps({'type': 'user', 'isSidechain': True, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u', 'content': 'file text'}]}}),
        ]
        self._add_agent('a1', 5, 'Map', body='\n'.join(lines) + '\n')
        self._add_agent('a2', 5, 'Fresh', body=first + '\n')

        texts = agent_texts(local_root(), _SESSION_ID, 'a1')
        self.assertEqual((texts['task'], texts['result']), ('The task', report))
        # Every step in order: the replies' texts and the call with its input and
        # result; the thinking stays unread.
        self.assertEqual([(step['kind'], step.get('text') or step.get('tool')) for step in texts['steps']],
                         [('text', 'Looking.'), ('text', report), ('call', 'Read')])
        call = texts['steps'][2]
        self.assertEqual((call['id'], call['input'], call['result'], call['error'], call['outcome']), ('u', {}, 'file text', False, ''))
        self.assertNotIn('never shown', json.dumps(texts))
        self.assertEqual(agent_texts(local_root(), _SESSION_ID, 'a2'), {'task': 'The task', 'result': '', 'steps': []})

    def test_a_handed_back_report_or_a_structured_output_is_the_result(self) -> None:
        first = json.dumps({'type': 'user', 'isSidechain': True, 'message': {'content': 'The task'}})

        def call(name, tool_input):
            return json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [
                {'type': 'tool_use', 'id': 'h', 'name': name, 'input': tool_input}]}})

        said = json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'Handing back now.'}]}})
        ends = json.dumps({'type': 'user', 'isSidechain': True, 'toolEndsTurn': True,
                           'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'h', 'content': 'Report delivered to your caller.'}]}})
        self._add_agent('bg', 5, 'Bg', body='\n'.join([first, said, call('SubagentHandback', {'message': '  The report.  '}), ends]) + '\n')
        self._add_agent('wf', 5, 'Wf', body='\n'.join([first, call('StructuredOutput', {'verdict': 'ok', 'count': 2}), ends]) + '\n')
        self._add_agent('empty', 5, 'Empty', body='\n'.join([first, call('SubagentHandback', {'message': ' '}), said]) + '\n')

        self.assertEqual(agent_texts(local_root(), _SESSION_ID, 'bg')['result'], 'The report.')
        self.assertEqual(json.loads(agent_texts(local_root(), _SESSION_ID, 'wf')['result']), {'verdict': 'ok', 'count': 2})
        self.assertEqual(agent_texts(local_root(), _SESSION_ID, 'empty')['result'], 'Handing back now.')

    def test_each_step_says_how_it_ended(self) -> None:
        first = json.dumps({'type': 'user', 'isSidechain': True, 'message': {'content': 'The task'}})
        calls = json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [
            {'type': 'tool_use', 'id': f'u{index}', 'name': 'Bash', 'input': {}} for index in range(6)]}})

        def result(tool_use_id, text, is_error, **extra):
            return json.dumps({'type': 'user', 'isSidechain': True, **extra, 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': tool_use_id, 'is_error': is_error, 'content': text}]}})

        lines = [first, calls, result('u0', 'Exit code 127\nnot found', True), result('u1', "The user doesn't want to proceed with this tool use.", True),
                 result('u2', 'partial', False, toolUseResult={'interrupted': True}), result('u3', 'ok', False),
                 result('u4', 'Permission for this action was denied by the classifier.', True),
                 result('u5', '[Tool call interrupted: the session ended before it finished]', True)]
        self._add_agent('a1', 5, 'Map', body='\n'.join(lines) + '\n')

        steps = agent_texts(local_root(), _SESSION_ID, 'a1')['steps']

        self.assertEqual([(step['outcome'], step['exit_code'], step['error']) for step in steps],
                         [('failed', 127, True), ('rejected', None, False), ('interrupted', None, False), ('', None, False),
                          ('rejected', None, False), ('interrupted', None, False)])

    def test_a_found_agent_transcript_is_not_searched_for_again(self) -> None:
        self._add_agent('a1', 5, 'Map', body=_assistant_end_turn())
        with mock.patch.object(smod, '_find_agent_file', wraps=smod._find_agent_file) as find:
            agent_texts(local_root(), _SESSION_ID, 'a1')
            agent_texts(local_root(), _SESSION_ID, 'a1')

        self.assertEqual(find.call_count, 1)

    def test_an_agent_id_that_could_name_a_path_is_refused(self) -> None:
        self._add_agent('a1', 5, 'Map', body=_assistant_end_turn())
        for agent_id in ('../a1', 'a1/../a1', '.a1', '', 'a1.jsonl', 'a' * 65):
            self.assertIsNone(_task(local_root(), _SESSION_ID, agent_id), agent_id)

    def test_no_directory_is_empty(self) -> None:
        self.assertEqual(count_subagents(local_root(), 'other-id', _CWD).running, 0)

    def test_agents_started_in_a_worktree_are_found_under_its_folder(self) -> None:
        # The session's transcript stays under the launch directory's folder, but
        # an agent it starts after switching into a worktree is filed under the
        # worktree's - both count, and a worktree folder named twice once.
        worktree = 'd:\\WebDev\\proj-worktrees\\fix'
        self._add_agent('a1', 5, 'Before the switch', body=_assistant_tool_use())
        worktree_dir = projects_dir(local_root()) / cwd_to_slug(worktree) / _SESSION_ID / 'subagents'
        worktree_dir.mkdir(parents=True)
        (worktree_dir / 'agent-a2.jsonl').write_text(_assistant_end_turn(), encoding='utf-8')

        info = count_subagents(local_root(), _SESSION_ID, _CWD, (worktree, worktree, 'd:\\WebDev\\gone'))

        self.assertEqual(info.running, 1)
        self.assertEqual(info.recent_done, 1)
        self.assertEqual(sorted(agent.agent_id for agent in info.agents), ['a1', 'a2'])
        self.assertEqual([agent.agent_id for agent in count_subagents(local_root(), _SESSION_ID, _CWD).agents], ['a1'])

    def test_executing_tool_is_running(self) -> None:
        # Last entry is an unanswered tool_use -> executing -> running, however
        # long the tool has been going (freshness alone must not drop it).
        self._add_agent('a', 5, 'Task A', body=_assistant_tool_use())
        self._add_agent('b', 400, 'Task B', body=_assistant_tool_use())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 2)
        self.assertEqual(info.recent_done, 0)
        self.assertEqual(set(info.labels), {'Task A', 'Task B'})

    def test_end_turn_is_finished(self) -> None:
        self._add_agent('c', 5, 'Task C', body=_assistant_end_turn())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 1)

    def test_settled_workflow_completion_is_finished(self) -> None:
        # The reported bug: a workflow agent finishes on a resolved StructuredOutput
        # tool call (no trailing end_turn) and, once quiet, must read as finished -
        # not stuck as running until the recent window expires.
        self._add_agent('wf', 120, 'WF done', body=_WORKFLOW_DONE_BODY)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 1)

    def test_fresh_answered_turn_is_still_running(self) -> None:
        # Same shape as a completed workflow agent, but written seconds ago: the
        # agent is between a tool_result and its next turn, so it must not be
        # misread as done (no flicker while it is still working).
        self._add_agent('think', 3, 'Thinking', body=_WORKFLOW_DONE_BODY)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 1)
        self.assertEqual(info.labels, ('Thinking',))

    def test_the_connection_view_lists_every_agent_newest_first(self) -> None:
        self._add_agent('fresh', 5, 'Fresh', body=_assistant_tool_use())
        self._add_agent('hour', 3600, 'An hour ago', body=_assistant_end_turn())
        self._add_agent('wf', 7200, 'In a workflow', body=_assistant_end_turn(), nested=True)
        self._add_agent('week', 7 * 86400, 'A week ago', body=_assistant_end_turn())
        self._add_agent('old', 31 * 86400, 'A month ago', body=_assistant_end_turn())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual([agent.agent_id for agent in info.agents], ['fresh', 'hour', 'wf', 'week', 'old'])
        self.assertEqual([agent.running for agent in info.agents], [True, False, False, False, False])
        self.assertEqual(info.agents[2].workflow, 'wf_1')
        self.assertEqual(info.agents[1].description, 'An hour ago')
        # Outside the recent window an agent no longer counts as recently finished.
        self.assertEqual((info.running, info.recent_done), (1, 0))

    def test_finds_nested_workflow_agents(self) -> None:
        self._add_agent('w', 3, 'Workflow agent', body=_assistant_tool_use(), nested=True)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 1)
        self.assertEqual(info.labels, ('Workflow agent',))

    def test_older_than_window_is_ignored(self) -> None:
        self._add_agent('old', 99999, 'Task Old', body=_assistant_tool_use())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 0)
        self.assertEqual(info.recent_done, 0)

    def test_never_surfaces_subagent_transcript_content(self) -> None:
        body = _assistant_tool_use('Read', extra_input={'note': 'SECRET_SUBAGENT_BODY'})
        self._add_agent('s', 2, 'Benign label', body=body)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        # The whole returned record, not just the labels, so a field added later
        # cannot carry content past this guard unnoticed.
        serialized = json.dumps(asdict(info))
        self.assertIn('Benign label', serialized)
        self.assertNotIn('SECRET_SUBAGENT_BODY', serialized)

    def _add_journal(self, run: str, started: list[str], done: list[str], age_seconds: float) -> None:
        directory = self._dir / 'workflows' / run
        directory.mkdir(parents=True, exist_ok=True)

        lines = [_line({'type': 'started', 'agentId': agent_id, 'key': 'v2:x'}) for agent_id in started]
        for agent_id in done:
            lines.append(_line({'type': 'result', 'agentId': agent_id, 'key': 'v2:x', 'result': {'summary': 'SECRET_WORKFLOW_RESULT'}}))

        journal = directory / 'journal.jsonl'
        journal.write_text('\n'.join(lines) + '\n', encoding='utf-8')

        mtime = time.time() - age_seconds
        os.utime(journal, (mtime, mtime))

    def test_workflow_total_and_active_from_journal(self) -> None:
        ids = [f'a{index}' for index in range(12)]
        self._add_journal('wf_1', started=ids, done=ids[:4], age_seconds=5)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(len(info.workflows), 1)
        workflow = info.workflows[0]
        self.assertEqual(workflow.run_id, 'wf_1')
        self.assertEqual(workflow.total, 12)
        self.assertEqual(workflow.done, 4)
        self.assertTrue(workflow.active)

    def test_workflow_all_done_is_active_within_grace_window(self) -> None:
        # Every started agent has returned (a fan-out phase just closed), but the
        # journal was written seconds ago - the workflow bridges the pause before
        # the next phase, so it must still read as active.
        ids = ['a', 'b', 'c']
        self._add_journal('wf_1', started=ids, done=ids, age_seconds=5)

        self.assertTrue(count_subagents(local_root(), _SESSION_ID, _CWD).workflows[0].active)

    def test_workflow_all_done_and_settled_is_inactive(self) -> None:
        # Same balance, but the journal has been quiet past the grace window - the
        # workflow has genuinely finished and must no longer read as active.
        ids = ['a', 'b', 'c']
        self._add_journal('wf_1', started=ids, done=ids, age_seconds=120)

        self.assertFalse(count_subagents(local_root(), _SESSION_ID, _CWD).workflows[0].active)

    def test_workflow_with_open_agent_stays_active_despite_quiet_journal(self) -> None:
        # An agent is still open (started, no result) during a long silent think,
        # so the journal has not been written for a while. total > done keeps the
        # workflow active regardless of journal age - a thinking agent must never
        # flip the session to "your turn", the same trap the main status rule avoids.
        self._add_journal('wf_1', started=['a', 'b'], done=['a'], age_seconds=300)

        self.assertTrue(count_subagents(local_root(), _SESSION_ID, _CWD).workflows[0].active)

    def test_workflow_older_than_window_is_dropped(self) -> None:
        self._add_journal('wf_1', started=['a'], done=['a'], age_seconds=99999)

        self.assertEqual(count_subagents(local_root(), _SESSION_ID, _CWD).workflows, ())

    def test_workflow_journal_never_surfaces_result_content(self) -> None:
        self._add_journal('wf_1', started=['a'], done=['a'], age_seconds=5)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        serialized = json.dumps([(w.run_id, w.total, w.done, w.active) for w in info.workflows])
        self.assertNotIn('SECRET_WORKFLOW_RESULT', serialized)

    # The freshest running agent's file age - the activity the session's own
    # transcript cannot see, because a delegated turn appends nothing to it
    # while the agent works.

    def test_running_age_reports_the_freshest_running_agent(self) -> None:
        self._add_agent('old', 200, 'Old task', body=_assistant_tool_use())
        self._add_agent('fresh', 4, 'Fresh task', body=_assistant_tool_use('Bash', 'tu_2'))

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running, 2)
        self.assertIsNotNone(info.running_age)
        self.assertLess(info.running_age, 30)

    def test_running_age_ignores_finished_agents(self) -> None:
        # A returned agent hands the turn back, so the main transcript carries
        # the conversation again and its own age is the honest one.
        self._add_agent('done', 3, 'Done task', body=_assistant_end_turn())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.recent_done, 1)
        self.assertIsNone(info.running_age)

    def test_running_age_is_none_without_subagents(self) -> None:
        self.assertIsNone(count_subagents(local_root(), _SESSION_ID, _CWD).running_age)

    def test_running_age_clamps_a_file_dated_ahead_of_now(self) -> None:
        # Clock skew must not produce a negative age (which would sort a session
        # ahead of everything genuinely fresh).
        self._add_agent('skewed', -120, 'Skewed', body=_assistant_tool_use())

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.running_age, 0.0)


if __name__ == '__main__':
    unittest.main()
