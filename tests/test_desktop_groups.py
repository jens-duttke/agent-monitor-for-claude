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


def _config(groups: list[dict[str, Any]], assignments: dict[str, str] | None = None, sections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    prefs: dict[str, Any] = {'dframe-group-scopes': {_SCOPE: {'groups': groups, 'assignments': assignments or {}, 'order': {}}}}
    if sections is not None:
        prefs['dframe-code-sections'] = {_SCOPE: {'sections': sections}}
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

    def test_group_missing_from_sections_goes_last(self) -> None:
        groups = [{'id': 'cg-a', 'name': 'A'}, {'id': 'cg-b', 'name': 'B'}]
        self.write_config(_config(groups, sections=[_section('cg-b', 'B', 1)]))
        self.assertEqual([g['name'] for g in self.read()['groups']], ['B', 'A'])

    def test_non_code_mode_groups_are_ignored(self) -> None:
        groups = [{'id': 'cg-a', 'name': 'A', 'mode': 'cowork'}, {'id': 'cg-b', 'name': 'B'}]
        self.write_config(_config(groups))
        self.assertEqual([g['id'] for g in self.read()['groups']], ['cg-b'])

    def test_non_uuid_cli_session_id_is_skipped(self) -> None:
        self.write_config(_config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-1'}))
        self.write_session(_D1, '../../etc/passwd')
        self.assertEqual(self.read()['members'], {})

    def test_malformed_shapes_degrade(self) -> None:
        shapes: list[Any] = [
            '[]', 'not json', {'preferences': {}}, {'preferences': {'epitaxyPrefs': {'dframe-group-scopes': 'x'}}},
            _config([{'id': 'cg-1', 'name': 'G'}], {f'code:local_{_D1}': 'cg-unknown', 'bad': 5}),
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                desktop_groups._cache.clear()
                self.write_config(shape)
                self.write_session(_D1, _S1)
                result = self.read()
                self.assertEqual(set(result), {'groups', 'members', 'archived'})
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
            self.assertEqual(desktop_groups.desktop_groups(), {'groups': [], 'members': {}, 'archived': []})


if __name__ == '__main__':
    unittest.main()
