"""Tests for a session's cost parts: the summing helpers, the per-skill and web-search reads, and the subagent usage."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript
from agent_monitor_for_claude.history import list_history
from agent_monitor_for_claude.paths import cwd_to_slug, local_root, projects_dir, transcript_path
from agent_monitor_for_claude.snapshot import build_snapshot
from agent_monitor_for_claude.subagents import count_subagents, past_subagent_usage
from agent_monitor_for_claude.transcript import _absorb_line, _ScanState, prune_scan_cache, usage_scan, usage_scan_once
from agent_monitor_for_claude.usage_parts import UsageScan, merge_scans, session_usage

_SESSION_ID = '0a0b0c0d-1111-2222-3333-444444444444'
_CWD = 'd:\\WebDev\\costs'


def _reply(message_id: str, output: int, model: str = 'claude-opus-5-5', skill: str | None = None, **extra) -> str:
    entry = {
        'type': 'assistant', 'timestamp': '2026-11-04T10:00:00Z',
        'message': {'id': message_id, 'model': model, 'stop_reason': 'end_turn',
                    'usage': {'input_tokens': 10, 'output_tokens': output, 'cache_read_input_tokens': 100},
                    'content': [{'type': 'text', 'text': 'x'}]},
    }
    if skill is not None:
        entry['attributionSkill'] = skill
    entry.update(extra)
    return json.dumps(entry)


def _web_search_result(count: object) -> str:
    return json.dumps({
        'type': 'user', 'timestamp': '2026-11-04T10:00:05Z',
        'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'results'}]},
        'toolUseResult': {'query': 'q', 'results': [], 'durationSeconds': 1.5, 'searchCount': count},
    })


def _absorb(*lines: str, state: _ScanState | None = None) -> _ScanState:
    state = state or _ScanState()
    for line in lines:
        _absorb_line(line.encode('utf-8'), state)
    return state


class UsagePartsTest(unittest.TestCase):
    def test_session_usage_adds_the_agents_to_the_main_conversation(self) -> None:
        main = UsageScan(by_model={'opus': {'input_tokens': 1, 'output_tokens': 2}}, web_searches=1)
        agents = {
            'next:reviewer': UsageScan(by_model={'opus': {'input_tokens': 10}, 'haiku': {'output_tokens': 5}}, web_searches=2),
        }

        result = session_usage(main, agents)

        self.assertEqual(result['usage_by_model'], {'opus': {'input_tokens': 11, 'output_tokens': 2}, 'haiku': {'output_tokens': 5}})
        self.assertEqual(result['usage'], {'input_tokens': 11, 'output_tokens': 7})
        self.assertEqual(result['usage_parts']['main'], {'opus': {'input_tokens': 1, 'output_tokens': 2}})
        self.assertEqual(result['usage_parts']['agents']['next:reviewer']['haiku'], {'output_tokens': 5})
        self.assertEqual(result['usage_parts']['web_searches'], 3)

    def test_skills_are_summed_over_the_main_conversation_and_the_agents(self) -> None:
        main = UsageScan(by_skill={'review': {'opus': {'output_tokens': 1}}})
        agents = {'': UsageScan(by_skill={'review': {'opus': {'output_tokens': 4}}, 'next:review-fix': {'opus': {'output_tokens': 2}}})}

        skills = session_usage(main, agents)['usage_parts']['skills']

        self.assertEqual(skills['review']['opus']['output_tokens'], 5)
        self.assertEqual(skills['next:review-fix']['opus']['output_tokens'], 2)

    def test_a_pending_main_scan_leaves_only_the_agents(self) -> None:
        result = session_usage(None, {'x': UsageScan(by_model={'opus': {'input_tokens': 3}})})

        self.assertEqual(result['usage'], {'input_tokens': 3})
        self.assertEqual(result['usage_parts']['main'], {})

    def test_the_result_shares_no_dict_with_its_inputs(self) -> None:
        main = UsageScan(by_model={'opus': {'input_tokens': 1}})

        result = session_usage(main, {})
        result['usage_by_model']['opus']['input_tokens'] = 99
        result['usage_parts']['main']['opus']['input_tokens'] = 98

        self.assertEqual(main.by_model['opus']['input_tokens'], 1)

    def test_merge_of_nothing_is_empty(self) -> None:
        self.assertEqual(merge_scans([]), UsageScan())


class SkillAndSearchReadTest(unittest.TestCase):
    def test_a_reply_is_counted_under_the_skill_it_ran_under(self) -> None:
        state = _absorb(_reply('m1', 5, skill='next:review'), _reply('m2', 7))

        self.assertEqual(state.by_skill['next:review']['claude-opus-5-5']['output_tokens'], 5)
        self.assertEqual(state.by_model['claude-opus-5-5']['output_tokens'], 12)

    def test_a_skill_counts_a_split_response_once_like_the_total(self) -> None:
        # One response stored as two entries repeats its usage; the second may
        # only add the output it grew by - for the skill exactly as for the model.
        state = _absorb(_reply('m1', 5, skill='review'), _reply('m1', 8, skill='review'))

        self.assertEqual(state.by_skill['review']['claude-opus-5-5']['output_tokens'], 8)
        self.assertEqual(state.by_skill['review']['claude-opus-5-5']['input_tokens'], 10)

    def test_a_skill_name_of_another_shape_is_not_attributed(self) -> None:
        state = _absorb(_reply('m1', 5, skill='bad name <script>'), _reply('m2', 5, skill=''), _reply('m3', 5, skill=['x']))

        self.assertEqual(state.by_skill, {})
        self.assertEqual(state.by_model['claude-opus-5-5']['output_tokens'], 15)

    def test_a_skill_name_with_a_trailing_line_break_is_not_attributed(self) -> None:
        state = _absorb(_reply('m1', 5, skill='review\n'))

        self.assertEqual(state.by_skill, {})

    def test_the_synthetic_model_is_not_attributed_to_a_skill(self) -> None:
        state = _absorb(_reply('m1', 0, model='<synthetic>', skill='review'))

        self.assertEqual(state.by_skill, {})

    def test_web_searches_are_counted_from_the_result(self) -> None:
        state = _absorb(_web_search_result(3), _web_search_result(2))

        self.assertEqual(state.web_searches, 5)

    def test_a_malformed_search_count_counts_nothing(self) -> None:
        state = _absorb(_web_search_result(True), _web_search_result(-1), _web_search_result('4'), _web_search_result(None))

        self.assertEqual(state.web_searches, 0)

    def test_a_copy_keeps_the_new_fields_apart(self) -> None:
        state = _absorb(_reply('m1', 5, skill='review'), _web_search_result(1))

        copy = state.copy()
        _absorb(_reply('m2', 5, skill='review'), _web_search_result(1), state=copy)

        self.assertEqual(state.by_skill['review']['claude-opus-5-5']['output_tokens'], 5)
        self.assertEqual(state.web_searches, 1)
        self.assertEqual(copy.web_searches, 2)


class _ConfigDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        prune_scan_cache([])

    def tearDown(self) -> None:
        prune_scan_cache([])
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write_transcript(self, lines: list[str]) -> Path:
        path = transcript_path(local_root(), _SESSION_ID, _CWD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def _add_agent(self, name: str, agent_type: str | None, lines: list[str], age_seconds: float = 0, nested: bool = False) -> Path:
        directory = projects_dir(local_root()) / cwd_to_slug(_CWD) / _SESSION_ID / 'subagents'
        if nested:
            directory = directory / 'workflows' / 'wf_1'
        directory.mkdir(parents=True, exist_ok=True)
        agent = directory / f'agent-{name}.jsonl'
        agent.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        if agent_type is not None:
            (directory / f'agent-{name}.meta.json').write_text(json.dumps({'agentType': agent_type, 'description': 'd'}), encoding='utf-8')
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(agent, (stamp, stamp))
        return agent


class SubagentUsageTest(_ConfigDirTest):
    def test_every_agent_is_summed_per_type_however_long_ago_it_finished(self) -> None:
        self._add_agent('a', 'next:reviewer', [_reply('a1', 4, skill='next:review')], age_seconds=86400)
        self._add_agent('b', 'next:reviewer', [_reply('b1', 6)])
        self._add_agent('c', 'Explore', [_reply('c1', 1, model='claude-haiku-5-5')], nested=True)

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.usage_by_type['next:reviewer'].by_model['claude-opus-5-5']['output_tokens'], 10)
        self.assertEqual(info.usage_by_type['next:reviewer'].by_skill['next:review']['claude-opus-5-5']['output_tokens'], 4)
        self.assertEqual(info.usage_by_type['Explore'].by_model['claude-haiku-5-5']['output_tokens'], 1)
        self.assertFalse(info.usage_pending)

    def test_an_agent_without_metadata_is_summed_under_no_type(self) -> None:
        self._add_agent('a', None, [_reply('a1', 4)])

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.usage_by_type[''].by_model['claude-opus-5-5']['output_tokens'], 4)

    def test_an_agent_type_of_another_shape_is_summed_under_no_type(self) -> None:
        # The type reaches a tooltip, so only a name in Claude Code's own shape
        # is shown - one with a line break or markup is not.
        self._add_agent('a', 'bad\ntype <b>', [_reply('a1', 4)])

        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(list(info.usage_by_type), [''])

    def test_appended_usage_is_counted_on_the_next_poll(self) -> None:
        agent = self._add_agent('a', 'fork', [_reply('a1', 4)])
        count_subagents(local_root(), _SESSION_ID, _CWD)

        with agent.open('a', encoding='utf-8') as handle:
            handle.write(_reply('a2', 6) + '\n')
        info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(info.usage_by_type['fork'].by_model['claude-opus-5-5']['output_tokens'], 10)

    def test_a_large_agent_still_being_scanned_marks_the_usage_pending(self) -> None:
        self._add_agent('a', 'fork', [_reply('a1', 4)])

        with mock.patch.object(transcript, '_SYNC_SCAN_BYTES', 0), mock.patch.object(transcript, 'queue_scan'):
            info = count_subagents(local_root(), _SESSION_ID, _CWD)

        self.assertTrue(info.usage_pending)
        self.assertEqual(info.usage_by_type, {})

    def test_an_agent_prompt_is_never_taken_as_a_title(self) -> None:
        agent = self._add_agent('a', 'fork', [json.dumps({'type': 'user', 'message': {'content': 'agent prompt'}}), _reply('a1', 1)])

        usage_scan(agent)

        self.assertIsNone(transcript._scan_cache[os.path.normcase(str(agent))].title())

    def test_each_listed_agent_carries_its_own_usage_and_main_skill(self) -> None:
        self._add_agent('s', 'general-purpose', [_reply('s1', 3, 'claude-sonnet-5-5', skill='commit'), _reply('s2', 1, skill='review')])
        self._add_agent('p', 'Explore', [_reply('p1', 7)])

        agents = {agent.agent_id: agent for agent in count_subagents(local_root(), _SESSION_ID, _CWD).agents}

        self.assertEqual(agents['s'].skill, 'commit')
        self.assertEqual(agents['s'].usage_by_model['claude-sonnet-5-5']['output_tokens'], 3)
        self.assertEqual(agents['s'].usage_by_model['claude-opus-5-5']['output_tokens'], 1)
        self.assertEqual((agents['p'].skill, agents['p'].usage_by_model['claude-opus-5-5']['output_tokens']), ('', 7))
        # When it started comes through the cached scan, like its usage.
        self.assertEqual(agents['p'].started_at, datetime(2026, 11, 4, 10, tzinfo=timezone.utc).timestamp())
        self.assertEqual(agents['p'].tool_calls, 0)

    def test_merged_scans_add_tool_calls_and_keep_the_newest_use(self) -> None:
        merged = merge_scans([
            UsageScan(tools={'Bash': (2, 100.0, 1), 'Read': (1, None, 0)}, tool_calls=3),
            UsageScan(tools={'Bash': (3, 50.0, 2), 'Read': (2, 70.0, 0)}, tool_calls=5),
        ])

        self.assertEqual(merged.tools, {'Bash': (5, 100.0, 3), 'Read': (3, 70.0, 0)})
        self.assertEqual(merged.tool_calls, 8)

    def test_a_listed_agent_whose_scan_is_pending_carries_no_usage(self) -> None:
        self._add_agent('a', 'fork', [_reply('a1', 4, skill='commit')])

        with mock.patch.object(transcript, '_SYNC_SCAN_BYTES', 0), mock.patch.object(transcript, 'queue_scan'):
            agent = count_subagents(local_root(), _SESSION_ID, _CWD).agents[0]

        self.assertEqual((agent.skill, agent.usage_by_model), ('', {}))

    def test_pruning_keeps_the_agent_scans_of_a_live_session(self) -> None:
        self._write_transcript([_reply('m1', 1)])
        agent = self._add_agent('a', 'fork', [_reply('a1', 4)])
        usage_scan(agent)

        prune_scan_cache([(local_root(), _SESSION_ID, _CWD)])
        self.assertIn(os.path.normcase(str(agent)), transcript._scan_cache)

        prune_scan_cache([])
        self.assertNotIn(os.path.normcase(str(agent)), transcript._scan_cache)

    def test_pruning_keeps_the_agent_scans_a_session_left_in_a_worktree_folder(self) -> None:
        # An agent started after a switch into a worktree lies under the
        # worktree's project folder, in a folder named after the session.
        agent = Path(self._temp.name) / 'projects' / 'd--proj-worktrees-fix' / _SESSION_ID / 'subagents' / 'agent-w.jsonl'
        agent.parent.mkdir(parents=True)
        agent.write_text(_reply('w1', 2) + '\n', encoding='utf-8')
        stranger = Path(self._temp.name) / 'projects' / 'd--proj-worktrees-fix' / 'other-session' / 'subagents' / 'agent-x.jsonl'
        stranger.parent.mkdir(parents=True)
        stranger.write_text(_reply('x1', 2) + '\n', encoding='utf-8')
        usage_scan(agent)
        usage_scan(stranger)

        prune_scan_cache([(local_root(), _SESSION_ID, _CWD)])

        self.assertIn(os.path.normcase(str(agent)), transcript._scan_cache)
        self.assertNotIn(os.path.normcase(str(stranger)), transcript._scan_cache)

    def test_a_past_session_reads_its_agents_once_without_caching(self) -> None:
        path = self._write_transcript([_reply('m1', 1)])
        agent = self._add_agent('a', 'Explore', [_reply('a1', 4)])

        usage = past_subagent_usage(path)

        self.assertEqual(usage['Explore'].by_model['claude-opus-5-5']['output_tokens'], 4)
        self.assertNotIn(os.path.normcase(str(agent)), transcript._scan_cache)

    def test_a_past_session_reads_its_agents_in_a_worktree_folder_too(self) -> None:
        path = self._write_transcript([_reply('m1', 1)])
        self._add_agent('a', 'Explore', [_reply('a1', 4)])
        worktree = 'd:\\proj-worktrees\\fix'
        moved = projects_dir(local_root()) / cwd_to_slug(worktree) / _SESSION_ID / 'subagents'
        moved.mkdir(parents=True)
        (moved / 'agent-w.jsonl').write_text(_reply('w1', 6) + '\n', encoding='utf-8')

        before = past_subagent_usage(path)
        after = past_subagent_usage(path, (worktree, worktree, 'd:\\gone'))

        self.assertEqual(before['Explore'].by_model['claude-opus-5-5']['output_tokens'], 4)
        self.assertEqual(sum(scan.by_model['claude-opus-5-5']['output_tokens'] for scan in after.values()), 10)

    def test_an_unreadable_agent_counts_as_empty(self) -> None:
        self.assertEqual(usage_scan_once(Path(self._temp.name) / 'missing.jsonl'), UsageScan())


class SessionCostRecordTest(_ConfigDirTest):
    def _write_registry(self) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        sessions.mkdir(exist_ok=True)
        (sessions / 'live.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': _SESSION_ID, 'cwd': _CWD, 'kind': 'interactive'}), encoding='utf-8')

    def test_a_live_record_prices_its_subagents_too(self) -> None:
        self._write_registry()
        self._write_transcript([_reply('m1', 2, skill='review'), _web_search_result(2)])
        self._add_agent('a', 'next:reviewer', [_reply('a1', 30, skill='review')])

        with mock.patch('agent_monitor_for_claude.snapshot.session_roots', return_value=[local_root()]):
            record = next(s for s in build_snapshot()['sessions'] if s['session_id'] == _SESSION_ID)

        self.assertEqual(record['usage_by_model']['claude-opus-5-5']['output_tokens'], 32)
        self.assertEqual(record['usage']['output_tokens'], 32)
        self.assertEqual(record['usage_parts']['main']['claude-opus-5-5']['output_tokens'], 2)
        self.assertEqual(record['usage_parts']['agents']['next:reviewer']['claude-opus-5-5']['output_tokens'], 30)
        self.assertEqual(record['usage_parts']['skills']['review']['claude-opus-5-5']['output_tokens'], 32)
        self.assertEqual(record['usage_parts']['web_searches'], 2)
        self.assertFalse(record['scan_pending'])

    def test_a_history_record_prices_its_subagents_and_names_its_branch(self) -> None:
        self._write_transcript([
            json.dumps({'type': 'user', 'timestamp': '2026-11-04T09:00:00Z', 'cwd': _CWD, 'gitBranch': 'feature/PNE-1/x',
                        'message': {'content': 'hello'}}),
            _reply('m1', 2, gitBranch='feature/PNE-1/x'),
        ])
        self._add_agent('a', 'Explore', [_reply('a1', 5)])

        with mock.patch('agent_monitor_for_claude.history.session_roots', return_value=[local_root()]), \
                mock.patch('agent_monitor_for_claude.history.live_or_recent_ids', return_value=set()):
            record = list_history()[0]

        self.assertEqual(record['usage_by_model']['claude-opus-5-5']['output_tokens'], 7)
        self.assertEqual(record['usage_parts']['agents']['Explore']['claude-opus-5-5']['output_tokens'], 5)
        self.assertEqual(record['git_branch'], 'feature/PNE-1/x')


if __name__ == '__main__':
    unittest.main()
