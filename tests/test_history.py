"""
Tests for the session-history listing.

``history.list_history`` surfaces past, non-live sessions by walking the
``projects/`` transcripts, deduping against the live registry, and resolving
each session's correct title and working directory.  These tests cover the
enumeration, the dedup, the title precedence for a rename buried deep in a
file, the cwd recovery (and its slug fallback), graceful degradation, and (in
``MultiRootHistoryTest``) that the same scan runs per session root with each
root's cwd resolution kept strictly isolated from every other root's.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import history
from agent_monitor_for_claude.history import list_history
from agent_monitor_for_claude.paths import SessionRoot, windows_root

_LIVE_PID = 424242


class HistoryEnvTest(unittest.TestCase):
    """Isolated CLAUDE_CONFIG_DIR, with session roots pinned to the Windows fixture root.

    Mirrors ``test_snapshot.py``'s ``_RegistryFixture``: without this pin, a
    real running WSL distro on the machine running the suite would be
    discovered for real by ``session_roots()``, making these Windows-only
    tests slow and dependent on that machine's WSL state.  ``MultiRootHistoryTest``
    below overrides the pin explicitly for its own scope.
    """

    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name

        self.windows_root = windows_root()
        roots_patcher = mock.patch.object(history, 'session_roots', return_value=[self.windows_root])
        roots_patcher.start()
        self.addCleanup(roots_patcher.stop)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write_history_transcript(self, slug: str, session_id: str, lines: list[str]) -> Path:
        directory = Path(self._temp.name) / 'projects' / slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{session_id}.jsonl'
        path.write_text('\n'.join(lines), encoding='utf-8')
        return path

    def _write_session(self, session_id: str, cwd: str, pid: int, proc_start: str | None = None) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        sessions.mkdir(exist_ok=True)
        payload: dict = {'pid': pid, 'sessionId': session_id, 'cwd': cwd, 'name': session_id[:8], 'kind': 'interactive'}
        if proc_start is not None:
            payload['procStart'] = proc_start
        (sessions / f'{pid}.json').write_text(json.dumps(payload), encoding='utf-8')


class ListHistoryTest(HistoryEnvTest):
    def test_missing_projects_dir_yields_empty(self) -> None:
        self.assertEqual(list_history(), [])

    def test_lists_transcripts_as_non_live_history(self) -> None:
        self._write_history_transcript('d--proj-a', 'aaaaaaaa-1111-2222-3333-444444444444', [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj\\a',
                        'message': {'content': 'first prompt A'}}),
        ])
        self._write_history_transcript('d--proj-b', 'bbbbbbbb-1111-2222-3333-444444444444', [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj\\b',
                        'message': {'content': 'first prompt B'}}),
        ])

        records = list_history()

        self.assertEqual(len(records), 2)
        for record in records:
            self.assertTrue(record['is_history'])
            self.assertFalse(record['alive'])
            self.assertTrue(record['has_transcript'])

    def test_excludes_sessions_present_in_the_registry(self) -> None:
        # The live session's registry PID is genuinely alive, so the live snapshot
        # retains it and history must not double it; the dead one has no registry
        # record and belongs in history.
        live_id = 'cccccccc-1111-2222-3333-444444444444'
        self._write_history_transcript('d--proj', live_id, [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj',
                        'message': {'content': 'live one'}}),
        ])
        self._write_history_transcript('d--proj', 'dddddddd-1111-2222-3333-444444444444', [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj',
                        'message': {'content': 'dead one'}}),
        ])
        self._write_session(live_id, 'd:\\proj', os.getpid())

        records = list_history()
        ids = {record['session_id'] for record in records}

        self.assertNotIn(live_id, ids)
        self.assertIn('dddddddd-1111-2222-3333-444444444444', ids)

    def test_dead_registry_record_past_retention_still_shows_in_history(self) -> None:
        # A crashed/killed session can leave a registry record that was never
        # pruned. Once its last activity is past the retention window the live
        # snapshot drops it; it must still surface in history rather than vanish
        # from both views. Dedup is therefore against the sessions the live
        # snapshot actually retains, not every registry record.
        dead_id = 'dddddddd-1111-2222-3333-444444444444'
        self._write_history_transcript('d--proj', dead_id, [
            json.dumps({'type': 'user', 'timestamp': '2020-01-01T09:00:00Z', 'cwd': 'd:\\proj',
                        'message': {'content': 'crashed long ago'}}),
        ])
        # A live PID with a mismatched procStart is detected as PID reuse: not alive.
        self._write_session(dead_id, 'd:\\proj', os.getpid(), proc_start='1')

        ids = {record['session_id'] for record in list_history()}

        self.assertIn(dead_id, ids)

    def test_custom_title_deep_in_file_outranks_first_prompt(self) -> None:
        # A rename entry sits far past any head window; the whole-file scan must
        # still find it and let it outrank the auto title and the first prompt.
        filler = json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T11:00:00Z',
                             'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x' * 200}]}})
        lines = [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj',
                        'message': {'content': 'the first prompt'}}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Auto title'}),
        ]
        lines.extend([filler] * 1000)
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': 'Renamed by user'}))

        self._write_history_transcript('d--proj', 'eeeeeeee-1111-2222-3333-444444444444', lines)
        records = list_history()

        self.assertEqual(records[0]['title'], 'Renamed by user')

    def test_cwd_recovered_from_a_later_entry(self) -> None:
        # Newer transcripts open with a light metadata record that has no cwd;
        # the cwd sits in the first real turn and must still be recovered.
        lines = [
            json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}),
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\WebDev\\HexEd.it',
                        'message': {'content': 'a prompt'}}),
        ]
        self._write_history_transcript('d--WebDev-HexEd-it', 'ffffffff-1111-2222-3333-444444444444', lines)
        records = list_history()

        self.assertEqual(records[0]['cwd'], 'd:\\WebDev\\HexEd.it')

    def test_cwd_falls_back_to_slug_when_absent(self) -> None:
        # A transcript that never carries a cwd (an aborted session) falls back
        # to the project folder name so it still groups somewhere stable.
        lines = [
            json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}),
        ]
        self._write_history_transcript('d--orphan-proj', '99999999-1111-2222-3333-444444444444', lines)
        records = list_history()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['cwd'], 'd--orphan-proj')

    def test_cwd_less_session_inherits_a_sibling_transcript_cwd(self) -> None:
        # A minimal/aborted transcript carries no cwd of its own; it must inherit
        # its project's real path from a sibling in the same folder, so it groups
        # with the rest of the project instead of splitting off under the slug.
        slug = 'd--WebDev-oku3d-app'
        self._write_history_transcript(slug, 'aaaaaaaa-1111-2222-3333-444444444444', [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'D:\\WebDev\\oku3d-app',
                        'message': {'content': 'real session'}}),
        ])
        self._write_history_transcript(slug, 'bbbbbbbb-1111-2222-3333-444444444444', [
            json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}),
        ])

        by_id = {record['session_id']: record for record in list_history()}

        self.assertEqual(by_id['aaaaaaaa-1111-2222-3333-444444444444']['cwd'], 'D:\\WebDev\\oku3d-app')
        self.assertEqual(by_id['bbbbbbbb-1111-2222-3333-444444444444']['cwd'], 'D:\\WebDev\\oku3d-app')

    def test_cwd_less_session_inherits_the_live_registry_cwd(self) -> None:
        # When the project has a live session, its exact registry cwd is used, so
        # a cwd-less past session in the same folder merges into the live panel.
        cwd = 'D:\\WebDev\\oku3d-app'
        slug = 'd--WebDev-oku3d-app'
        self._write_session('cccccccc-1111-2222-3333-444444444444', cwd, _LIVE_PID)
        self._write_history_transcript(slug, 'dddddddd-1111-2222-3333-444444444444', [
            json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}),
        ])

        records = list_history()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['cwd'], cwd)

    def test_record_shape_is_content_free_and_complete(self) -> None:
        self._write_history_transcript('d--proj', '11111111-1111-2222-3333-444444444444', [
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'cwd': 'd:\\proj',
                        'message': {'content': 'hello'}}),
            json.dumps({'type': 'assistant', 'timestamp': '2026-07-11T09:00:10Z', 'cwd': 'd:\\proj',
                        'message': {'stop_reason': 'end_turn', 'model': 'claude-opus-4-8',
                                    'usage': {'input_tokens': 5, 'output_tokens': 3}, 'content': []}}),
        ])
        record = list_history()[0]

        self.assertEqual(record['model_id'], 'claude-opus-4-8')
        self.assertEqual(record['short_name'], '11111111')
        self.assertIsNotNone(record['age_seconds'])
        self.assertEqual(record['usage'], {})
        self.assertEqual(record['subagents_running'], 0)


class MultiRootHistoryTest(HistoryEnvTest):
    """History scanning across the Windows fixture root and a fake WSL root together.

    The WSL side gets its own temp dir standing in for a distro's ``.claude``
    (``config_dir``), built the same way ``test_snapshot.py``'s
    ``MultiRootSnapshotTest`` builds one - here with ``history.session_roots``
    patched (overriding the single-root pin from ``HistoryEnvTest.setUp``) to
    return both roots together, and ``history.live_or_recent_ids`` stubbed so
    these tests never depend on real process-liveness state.
    """

    def test_history_lists_wsl_root_with_own_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            wsl_root = SessionRoot(
                origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp',
            )
            project = wsl_root.config_dir / 'projects' / '-home-dev-proj'
            project.mkdir(parents=True)
            (project / 'aaaaaaaa-1111-2222-3333-444444444444.jsonl').write_text(
                json.dumps({'type': 'user', 'cwd': '/home/dev/proj', 'timestamp': '2026-07-01T10:00:00Z',
                            'message': {'content': 'hello wsl'}}) + '\n', encoding='utf-8')

            with mock.patch.object(history, 'session_roots', return_value=[self.windows_root, wsl_root]), \
                 mock.patch.object(history, 'live_or_recent_ids', return_value=set()):
                records = history.list_history()

        wsl_records = [r for r in records if r['origin'] == 'wsl:U']
        self.assertEqual(len(wsl_records), 1)
        self.assertEqual(wsl_records[0]['cwd'], '/home/dev/proj')
        self.assertEqual(wsl_records[0]['origin_label'], 'U')

    def test_same_slug_does_not_leak_cwd_across_roots(self) -> None:
        # A Windows project and a WSL project can land on the identical slug
        # string (the slug scheme is a lossy character substitution, blind to
        # which root produced it), but each root's slug-to-cwd resolution must
        # come only from that root's own registry: a Windows live cwd must
        # never canonicalize a WSL project, or vice versa.
        slug = '-home-dev-proj'
        windows_cwd = '\\home\\dev\\proj'
        self._write_session('bbbbbbbb-1111-2222-3333-444444444444', windows_cwd, _LIVE_PID)
        self._write_history_transcript(slug, 'aaaaaaaa-1111-2222-3333-444444444444', [
            json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}),
        ])

        with tempfile.TemporaryDirectory() as base:
            wsl_root = SessionRoot(
                origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp',
            )
            project = wsl_root.config_dir / 'projects' / slug
            project.mkdir(parents=True)
            (project / 'cccccccc-1111-2222-3333-444444444444.jsonl').write_text(
                json.dumps({'type': 'summary', 'operation': 'compact', 'sessionId': 'x'}) + '\n', encoding='utf-8')

            with mock.patch.object(history, 'session_roots', return_value=[self.windows_root, wsl_root]), \
                 mock.patch.object(history, 'live_or_recent_ids', return_value=set()):
                records = history.list_history()

        by_id = {record['session_id']: record for record in records}
        # The Windows-side cwd-less session correctly inherits the Windows registry's cwd...
        self.assertEqual(by_id['aaaaaaaa-1111-2222-3333-444444444444']['cwd'], windows_cwd)
        # ...but the WSL-side session sharing the same slug must not: with no WSL
        # registry entry of its own, it falls back to the raw slug, never the
        # Windows cwd that happens to canonicalize to the identical slug string.
        self.assertEqual(by_id['cccccccc-1111-2222-3333-444444444444']['cwd'], slug)


if __name__ == '__main__':
    unittest.main()
