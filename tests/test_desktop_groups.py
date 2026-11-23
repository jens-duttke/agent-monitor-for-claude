"""Tests for the Claude Desktop group and archive reader."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_monitor_for_claude import desktop_groups

_SCOPE = 'acct-1/org-1'
_D1 = '56b0fd04-1111-4222-8333-944455556666'
_D2 = '67c1fe15-2222-4333-8444-a55566667777'
_S1 = '11111111-aaaa-4bbb-8ccc-222222222222'
_S2 = '33333333-aaaa-4bbb-8ccc-444444444444'


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')


def _config(groups: list[dict[str, Any]], assignments: dict[str, str] | None = None, sections: list[dict[str, Any]] | None = None,
            starred: Any = None) -> dict[str, Any]:
    prefs: dict[str, Any] = {'dframe-group-scopes': {_SCOPE: {'groups': groups, 'assignments': assignments or {}, 'order': {}}}}
    if sections is not None:
        prefs['dframe-code-sections'] = {_SCOPE: {'sections': sections}}
    if starred is not None:
        prefs['starred-local-code-sessions'] = starred
    return {'preferences': {'epitaxyPrefs': prefs}}


def _section(group_id: str, name: str, order: int, collapsed: bool = False) -> dict[str, Any]:
    return {'id': group_id, 'kind': 'manual', 'name': name, 'order': order, 'collapsed': collapsed}


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        desktop_groups._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write_config(self, content: Any) -> None:
        _write(self.dir / 'claude_desktop_config.json', content)

    def write_session(self, desktop_id: str, cli_id: Any, archived: bool = False) -> None:
        _write(self.dir / 'claude-code-sessions' / 'acct-1' / 'org-1' / f'local_{desktop_id}.json', {'cliSessionId': cli_id, 'isArchived': archived})

    def read(self) -> dict[str, Any]:
        return desktop_groups.desktop_groups([self.dir])


class DesktopGroupsTest(_Fixture):
    def test_reads_groups_in_section_order(self) -> None:
        groups = [{'id': 'cg-a', 'name': 'A', 'mode': 'code'}, {'id': 'cg-b', 'name': 'B', 'mode': 'code'}]
        self.write_config(_config(groups, sections=[_section('cg-b', 'B', 1), _section('cg-a', 'A', 2, collapsed=True)]))
        result = self.read()
        self.assertEqual([g['name'] for g in result['groups']], ['B', 'A'])
        orders = [g['order'] for g in result['groups']]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual([g['collapsed'] for g in result['groups']], [False, True])

    def test_members_map_desktop_id_to_cli_session_id(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G', 'mode': 'code'}], {f'code:local_{_D1}': 'cg-1'}))
        self.write_session(_D1, _S1)
        self.assertEqual(self.read()['members'], {_S1: 'cg-1'})

    def test_archived_sessions_are_listed_and_not_members(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G', 'mode': 'code'}], {f'code:local_{_D1}': 'cg-1', f'code:local_{_D2}': 'cg-1'}))
        self.write_session(_D1, _S1, archived=True)
        self.write_session(_D2, _S2)
        result = self.read()
        self.assertEqual(result['archived'], [_S1])
        self.assertEqual(result['members'], {_S2: 'cg-1'})

    def test_a_session_with_an_active_and_an_archived_file_is_not_archived(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1', f'code:local_{_D2}': 'cg-1'}))
        self.write_session(_D1, _S1, archived=True)
        self.write_session(_D2, _S1)
        result = self.read()
        self.assertEqual(result['archived'], [])
        self.assertEqual(result['members'], {_S1: 'cg-1'})

    def test_group_missing_from_sections_goes_last(self) -> None:
        groups = [{'id': 'cg-a', 'name': 'A'}, {'id': 'cg-b', 'name': 'B'}]
        self.write_config(_config(groups, sections=[_section('cg-b', 'B', 1)]))
        self.assertEqual([g['name'] for g in self.read()['groups']], ['B', 'A'])

    def test_non_code_mode_groups_are_ignored(self) -> None:
        groups = [{'id': 'cg-a', 'name': 'A', 'mode': 'cowork'}, {'id': 'cg-b', 'name': 'B'}]
        self.write_config(_config(groups))
        self.assertEqual([g['id'] for g in self.read()['groups']], ['cg-b'])

    def test_an_ended_chat_falls_back_to_the_session_it_ran_last(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1', f'code:local_{_D2}': 'cg-1'}))
        last = '11111111-2222-4333-8444-555555555555'
        prior = ['66666666-7777-4888-8999-aaaaaaaaaaaa', '77777777-8888-4999-8aaa-bbbbbbbbbbbb']
        path = self.dir / 'claude-code-sessions' / 'acct-1' / 'org-1'
        _write(path / f'local_{_D1}.json', {'lastAssistantCliSessionId': last, 'priorCliSessionIds': prior, 'isArchived': False})
        _write(path / f'local_{_D2}.json', {'cliSessionId': None, 'priorCliSessionIds': prior, 'isArchived': False})
        self.assertEqual(self.read()['members'], {last: 'cg-1', prior[-1]: 'cg-1'})

    def test_non_uuid_cli_session_id_is_skipped(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1'}))
        self.write_session(_D1, '../../etc/passwd')
        self.assertEqual(self.read()['members'], {})

    def test_malformed_shapes_degrade(self) -> None:
        shapes: list[Any] = [
            '[]', 'not json', {'preferences': {}}, {'preferences': {'epitaxyPrefs': {'dframe-group-scopes': 'x'}}},
            _config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-unknown', 'bad': 5}),
            _config([{'id': ['cg-1'], 'name': 'G'}, {'id': {'x': 1}}], {f'code:local_{_D1}': ['cg-1'], f'code:local_{_D2}': {'a': 1}},
                    sections=[{'id': ['cg-1'], 'kind': 'manual', 'order': 1}, {'id': {'x': 1}, 'kind': 'manual', 'order': 2}]),
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                desktop_groups._cache.clear()
                self.write_config(shape)
                self.write_session(_D1, _S1)
                result = self.read()
                self.assertEqual(set(result), {'groups', 'members', 'archived', 'starred', 'lineage'})
                self.assertEqual(result['members'], {})

    def test_group_name_is_clipped(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'x' * 500}]))
        self.assertEqual(len(self.read()['groups'][0]['name']), desktop_groups._MAX_GROUP_NAME_CHARS)

    def test_unchanged_files_are_not_reread(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1'}))
        self.write_session(_D1, _S1)
        first = self.read()
        with mock.patch.object(desktop_groups, '_read_json', side_effect=AssertionError('reread')) as reader:
            second = self.read()
        self.assertEqual(first, second)
        reader.assert_not_called()

    def test_the_cache_keeps_only_the_extracted_values(self) -> None:
        config = _config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1'})
        config['mcpServers'] = {'srv': {'command': 'x', 'env': {'API_KEY': 'SECRET-CONFIG-VALUE'}}}
        config['preferences']['epitaxyPrefs']['otherPref'] = 'SECRET-PREF-VALUE'
        self.write_config(config)
        session_file = self.dir / 'claude-code-sessions' / 'acct-1' / 'org-1' / f'local_{_D1}.json'
        _write(session_file, {'cliSessionId': _S1, 'isArchived': True, 'title': 'SECRET-TITLE', 'postTurnSummary': 'SECRET-SUMMARY'})

        self.read()

        cached = repr(desktop_groups._cache)
        for marker in ('SECRET-CONFIG-VALUE', 'SECRET-PREF-VALUE', 'SECRET-TITLE', 'SECRET-SUMMARY', 'mcpServers', 'title'):
            self.assertNotIn(marker, cached)
        session_entry = next(value for key, value in desktop_groups._cache.items() if Path(key).name.startswith('local_'))
        self.assertEqual(tuple(session_entry[2]), (_S1, True, None, None))
        config_entry = desktop_groups._cache[str(self.dir / 'claude_desktop_config.json')]
        self.assertEqual(set(config_entry[2]), {'dframe-group-scopes'})

    def test_a_changed_file_is_read_again(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}]))
        self.assertEqual(self.read()['groups'][0]['name'], 'G')
        self.write_config(_config([{'id': 'cg-1', 'name': 'Renamed group'}]))
        self.assertEqual(self.read()['groups'][0]['name'], 'Renamed group')

    def test_never_writes(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1'}))
        self.write_session(_D1, _S1)
        before = {str(p): p.stat().st_mtime_ns for p in self.dir.rglob('*')}
        self.read()
        self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in self.dir.rglob('*')})

    def test_no_dirs_gives_empty_result(self) -> None:
        with mock.patch.object(desktop_groups, 'desktop_data_dirs', return_value=[]):
            self.assertEqual(desktop_groups.desktop_groups(), {'groups': [], 'members': {}, 'archived': [], 'starred': [], 'lineage': []})


class DesktopStarredTest(_Fixture):
    def test_starred_sessions_map_to_cli_session_ids_in_desktop_order(self) -> None:
        self.write_config(_config([], starred=[f'local_{_D2}', f'local_{_D1}', f'local_{_D2}']))
        self.write_session(_D1, _S1)
        self.write_session(_D2, _S2)
        self.assertEqual(self.read()['starred'], [_S2, _S1])

    def test_an_archived_starred_session_stays_starred(self) -> None:
        self.write_config(_config([], starred=[f'local_{_D1}']))
        self.write_session(_D1, _S1, archived=True)
        result = self.read()
        self.assertEqual(result['starred'], [_S1])
        self.assertEqual(result['archived'], [_S1])

    def test_an_ended_starred_chat_falls_back_to_the_session_it_ran_last(self) -> None:
        self.write_config(_config([], starred=[f'local_{_D1}']))
        last = '11111111-2222-4333-8444-555555555555'
        _write(self.dir / 'claude-code-sessions' / 'acct-1' / 'org-1' / f'local_{_D1}.json', {'cliSessionId': None, 'lastAssistantCliSessionId': last})
        self.assertEqual(self.read()['starred'], [last])

    def test_an_unmapped_starred_id_is_skipped(self) -> None:
        self.write_config(_config([], starred=[f'local_{_D2}', f'local_{_D1}']))
        self.write_session(_D1, _S1)
        self.assertEqual(self.read()['starred'], [_S1])

    def test_malformed_starred_shapes_degrade(self) -> None:
        self.write_session(_D1, _S1)
        shapes: list[Any] = ['x', {'a': f'local_{_D1}'}, 5, [5, None, ['x'], f'code:local_{_D1}', _D1, 'local_../../x', f'local_{_D1}x']]
        for shape in shapes:
            with self.subTest(shape=shape):
                desktop_groups._cache.clear()
                self.write_config(_config([], starred=shape))
                self.assertEqual(self.read()['starred'], [])

    def test_starred_ids_merge_across_config_files(self) -> None:
        self.write_config(_config([], starred=[f'local_{_D1}']))
        self.write_session(_D1, _S1)
        other = self.dir / 'other'
        _write(other / 'claude_desktop_config.json', _config([], starred=[f'local_{_D2}', f'local_{_D1}']))
        _write(other / 'claude-code-sessions' / 'a' / 'o' / f'local_{_D2}.json', {'cliSessionId': _S2})
        self.assertEqual(desktop_groups.desktop_groups([self.dir, other])['starred'], [_S1, _S2])


_D3 = '78d20f26-3333-4444-8555-b66677778888'
_S3 = '55555555-aaaa-4bbb-8ccc-666666666666'


class DesktopLineageTest(_Fixture):
    def session_file(self, desktop_id: str, content: dict[str, Any]) -> None:
        _write(self.dir / 'claude-code-sessions' / 'acct-1' / 'org-1' / f'local_{desktop_id}.json', content)

    def test_a_started_session_names_its_parent(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}', 'taskId': 'task_1', 'title': 'Do X'}})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S1, 'kind': 'started'}])

    def test_a_fork_is_told_apart(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'forkedFromSessionId': f'local_{_D1}',
                                'spawnedFrom': {'sessionId': f'local_{_D1}', 'userFork': True}})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S1, 'kind': 'forked'}])

    def test_a_fork_named_only_at_the_top_level(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'forkedFromSessionId': f'local_{_D1}'})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S1, 'kind': 'forked'}])

    def test_a_detached_link_is_not_shown(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}'}, 'lineageDetached': True, 'lineageDetachedBy': 'user'})
        self.assertEqual(self.read()['lineage'], [])

    def test_a_missing_parent_file_gives_no_link(self) -> None:
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        self.assertEqual(self.read()['lineage'], [])

    def test_a_parent_without_a_resolvable_cli_id_gives_no_link(self) -> None:
        self.session_file(_D1, {'cliSessionId': None, 'priorCliSessionIds': []})
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        self.assertEqual(self.read()['lineage'], [])

    def test_the_parent_resolves_through_its_fallback_ids(self) -> None:
        self.session_file(_D1, {'cliSessionId': None, 'lastAssistantCliSessionId': _S1})
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S1, 'kind': 'started'}])

    def test_a_session_naming_itself_is_no_link(self) -> None:
        self.session_file(_D1, {'cliSessionId': _S1, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        self.write_session(_D2, _S2)
        self.session_file(_D3, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D2}'}})
        self.assertEqual(self.read()['lineage'], [])

    def test_a_chain_and_a_cycle_are_passed_on_link_by_link(self) -> None:
        self.session_file(_D1, {'cliSessionId': _S1, 'spawnedFrom': {'sessionId': f'local_{_D3}'}})
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        self.session_file(_D3, {'cliSessionId': _S3, 'spawnedFrom': {'sessionId': f'local_{_D2}'}})
        links = {(link['session'], link['parent']) for link in self.read()['lineage']}
        self.assertEqual(links, {(_S1, _S3), (_S2, _S1), (_S3, _S2)})

    def test_an_active_file_wins_over_an_archived_one(self) -> None:
        self.write_session(_D1, _S1)
        self.write_session(_D3, _S3)
        self.session_file(_D2, {'cliSessionId': _S2, 'isArchived': True, 'spawnedFrom': {'sessionId': f'local_{_D1}'}})
        other = self.dir / 'claude-code-sessions' / 'acct-1' / 'org-2' / 'local_99999999-aaaa-4bbb-8ccc-dddddddddddd.json'
        _write(other, {'cliSessionId': _S2, 'spawnedFrom': {'sessionId': f'local_{_D3}', 'userFork': True}})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S3, 'kind': 'forked'}])

    def test_wrong_types_in_every_field_degrade(self) -> None:
        self.write_session(_D1, _S1)
        shapes: list[Any] = [
            {'spawnedFrom': 'local_' + _D1}, {'spawnedFrom': ['x']}, {'spawnedFrom': None},
            {'spawnedFrom': {'sessionId': 5}}, {'spawnedFrom': {'sessionId': ['local_' + _D1]}}, {'spawnedFrom': {'sessionId': _D1}},
            {'spawnedFrom': {'sessionId': 'local_../../x'}}, {'spawnedFrom': {'sessionId': f'local_{_D1}x'}},
            {'forkedFromSessionId': 5}, {'forkedFromSessionId': {'a': 1}}, {'forkedFromSessionId': _D1},
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                desktop_groups._cache.clear()
                self.session_file(_D2, {'cliSessionId': _S2, **shape})
                self.assertEqual(self.read()['lineage'], [])

    def test_odd_flag_types_read_as_unset(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'lineageDetached': 'true', 'spawnedFrom': {'sessionId': f'local_{_D1}', 'userFork': 'yes'}})
        self.assertEqual(self.read()['lineage'], [{'session': _S2, 'parent': _S1, 'kind': 'started'}])

    def test_the_task_text_beside_the_link_is_not_kept(self) -> None:
        self.write_session(_D1, _S1)
        self.session_file(_D2, {'cliSessionId': _S2, 'spawnedFrom': {
            'sessionId': f'local_{_D1}', 'taskId': 'SECRET-TASK-ID', 'title': 'SECRET-TASK-TITLE', 'name': 'SECRET-NAME', 'btw': True,
        }})
        result = self.read()
        cached = repr(desktop_groups._cache) + repr(result)
        for marker in ('SECRET-TASK-ID', 'SECRET-TASK-TITLE', 'SECRET-NAME'):
            self.assertNotIn(marker, cached)


if __name__ == '__main__':
    unittest.main()
