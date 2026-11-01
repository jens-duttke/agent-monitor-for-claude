"""Tests for filing a worktree session back under the project it was launched in."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript as tmod
from agent_monitor_for_claude import worktrees as wmod
from agent_monitor_for_claude.paths import SessionRoot, transcript_path
from agent_monitor_for_claude.worktrees import prune_worktree_cache, resolve_worktree

_SESSION_ID = '2ee860e9-a20e-4a39-bc18-3c87e9c6ec19'
_LAUNCH_CWD = 'd:\\WebDev\\app'
_WORKTREE_CWD = 'D:\\WebDev\\app-worktrees\\feature'


def _worktree_entry(path: str = _WORKTREE_CWD, **extra: object) -> dict:
    session = {'originalCwd': 'D:\\WebDev\\app', 'preEnterOriginalCwd': _LAUNCH_CWD, 'worktreeName': 'feature', 'worktreePath': path}
    session.update(extra)
    return {'type': 'worktree-state', 'sessionId': _SESSION_ID, 'worktreeSession': session}


class ResolveWorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = SessionRoot(origin='local', label=None, config_dir=Path(temp.name), proc_dir=None, claude_temp_dir=Path(temp.name))
        (Path(temp.name) / 'projects').mkdir()

        wmod._resolutions.clear()
        tmod._scan_cache.clear()
        self.addCleanup(wmod._resolutions.clear)
        self.addCleanup(tmod._scan_cache.clear)

    def _record(self, cwd: str = _WORKTREE_CWD) -> dict:
        return {'session_id': _SESSION_ID, 'cwd': cwd, 'pid': 1, 'origin': 'local'}

    def _write_transcript(self, cwd: str, *entries: dict) -> Path:
        path = transcript_path(self.root, _SESSION_ID, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(entry) + '\n' for entry in entries), encoding='utf-8')
        return path

    def test_a_worktree_session_is_filed_under_its_launch_project(self) -> None:
        self._write_transcript(_LAUNCH_CWD, _worktree_entry(worktreeBranch='wt/feature'))

        record = resolve_worktree(self.root, self._record())

        # The launch directory whose slug is the folder itself, exactly - every
        # path later built from it (subagents, task output, scratchpad) must be
        # the one on disk.
        self.assertEqual(record['cwd'], _LAUNCH_CWD)
        self.assertEqual(record['worktree_cwd'], _WORKTREE_CWD)
        self.assertEqual(record['worktree_branch'], 'wt/feature')

    def test_a_session_whose_transcript_sits_under_its_cwd_is_left_alone(self) -> None:
        self._write_transcript(_WORKTREE_CWD, _worktree_entry())
        record = self._record()

        self.assertIs(resolve_worktree(self.root, record), record)

    def test_a_transcript_without_a_worktree_state_is_left_alone(self) -> None:
        # Found under another project, but nothing ties it to the registry's
        # cwd - filing it there would be a guess.
        self._write_transcript(_LAUNCH_CWD, {'type': 'user', 'message': {'content': 'x'}})
        record = self._record()

        self.assertIs(resolve_worktree(self.root, record), record)

    def test_a_launch_directory_that_is_not_the_folder_is_refused(self) -> None:
        # The transcript is filed under a folder neither launch directory maps
        # to; the record would point at a project that holds nothing.
        path = transcript_path(self.root, _SESSION_ID, 'd:\\Elsewhere')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_worktree_entry()) + '\n', encoding='utf-8')
        record = self._record()

        self.assertIs(resolve_worktree(self.root, record), record)

    def test_a_drive_letter_in_the_other_case_matches_where_the_file_system_agrees(self) -> None:
        # Claude Code records the drive letter in either case. A case-insensitive
        # file system reaches the transcript through the other spelling, so it is
        # the same folder; a case-sensitive one does not, and filing the session
        # there would point every reader at a folder that holds nothing.
        entry = _worktree_entry()
        entry['worktreeSession'].pop('preEnterOriginalCwd')
        self._write_transcript(_LAUNCH_CWD, entry)
        case_insensitive = transcript_path(self.root, _SESSION_ID, 'D:\\WebDev\\app').is_file()

        record = resolve_worktree(self.root, self._record())

        self.assertEqual(record['cwd'], 'D:\\WebDev\\app' if case_insensitive else _WORKTREE_CWD)

    def test_a_branch_is_reported_only_for_the_worktree_the_registry_names(self) -> None:
        # The registry may already name the next worktree while the transcript
        # still records the previous one; its branch would be the wrong one.
        self._write_transcript(_LAUNCH_CWD, _worktree_entry(path='D:\\WebDev\\app-worktrees\\previous', worktreeBranch='wt/previous'))

        record = resolve_worktree(self.root, self._record())

        self.assertEqual(record['cwd'], _LAUNCH_CWD)
        self.assertIsNone(record['worktree_branch'])

    def test_a_hit_is_remembered_and_not_searched_again(self) -> None:
        self._write_transcript(_LAUNCH_CWD, _worktree_entry())

        with mock.patch.object(wmod, 'find_transcript', wraps=wmod.find_transcript) as find:
            for _ in range(3):
                resolve_worktree(self.root, self._record())

        self.assertEqual(find.call_count, 1)

    def test_a_hit_whose_transcript_is_gone_is_searched_again(self) -> None:
        path = self._write_transcript(_LAUNCH_CWD, _worktree_entry())
        resolve_worktree(self.root, self._record())
        path.unlink()

        record = self._record()
        self.assertIs(resolve_worktree(self.root, record), record)

    def test_a_miss_is_retried_after_a_while(self) -> None:
        # A miss is usually a new session with no transcript yet, or a registry
        # that names the worktree before the transcript records the switch - so
        # it is retried rather than kept, but not on every poll.
        record = self._record()
        with mock.patch.object(wmod.time, 'monotonic', return_value=1000.0):
            self.assertIs(resolve_worktree(self.root, record), record)

        self._write_transcript(_LAUNCH_CWD, _worktree_entry())

        with mock.patch.object(wmod.time, 'monotonic', return_value=1000.0 + wmod._RETRY_SECONDS - 1):
            self.assertIs(resolve_worktree(self.root, record), record)
        with mock.patch.object(wmod.time, 'monotonic', return_value=1000.0 + wmod._RETRY_SECONDS + 1):
            self.assertEqual(resolve_worktree(self.root, record)['cwd'], _LAUNCH_CWD)

    def test_prune_drops_lookups_of_records_that_are_gone(self) -> None:
        self._write_transcript(_LAUNCH_CWD, _worktree_entry())
        resolve_worktree(self.root, self._record())

        prune_worktree_cache([('local', _SESSION_ID, _WORKTREE_CWD)])
        self.assertEqual(len(wmod._resolutions), 1)

        prune_worktree_cache([])
        self.assertEqual(wmod._resolutions, {})


if __name__ == '__main__':
    unittest.main()
