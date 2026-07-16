"""
Tests for session deletion - the application's only sanctioned write surface.

``session_delete.delete_session`` removes a past session's transcript and
subagent folder.  These tests cover the three guards (UUID validation, the
live-process refusal, path confinement), the actual removal, and idempotency.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude.paths import cwd_to_slug
from agent_monitor_for_claude.session_delete import delete_session, _within

_CWD = 'd:\\PythonDev\\demo-proj'
_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
_OTHER_UUID = '11111111-2222-3333-4444-555555555555'


class DeleteEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _project_dir(self, cwd: str) -> Path:
        return Path(self._temp.name) / 'projects' / cwd_to_slug(cwd)

    def _write_session_files(self, cwd: str, session_id: str) -> tuple[Path, Path]:
        project = self._project_dir(cwd)
        subagents = project / session_id / 'subagents'
        subagents.mkdir(parents=True, exist_ok=True)

        transcript = project / f'{session_id}.jsonl'
        transcript.write_text(
            json.dumps({'type': 'user', 'timestamp': '2026-07-11T09:00:00Z', 'message': {'content': 'x'}}),
            encoding='utf-8',
        )
        (subagents / 'agent-1.jsonl').write_text('{}', encoding='utf-8')

        return transcript, project / session_id

    def _write_registry(self, cwd: str, session_id: str, pid: int) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        sessions.mkdir(exist_ok=True)
        (sessions / f'{pid}.json').write_text(
            json.dumps({'pid': pid, 'sessionId': session_id, 'cwd': cwd, 'name': session_id[:8], 'kind': 'interactive'}),
            encoding='utf-8',
        )


class DeleteSessionTest(DeleteEnvTest):
    def test_rejects_non_uuid_session_id(self) -> None:
        transcript, _ = self._write_session_files(_CWD, _UUID)
        # A traversal-flavoured id must be refused outright by the UUID guard.
        self.assertFalse(delete_session('..\\..\\evil', _CWD))
        self.assertTrue(transcript.is_file())

    def test_rejects_non_string_cwd(self) -> None:
        self.assertFalse(delete_session(_UUID, None))  # type: ignore[arg-type]

    def test_refuses_to_delete_a_live_session(self) -> None:
        transcript, session_dir = self._write_session_files(_CWD, _UUID)
        # No procStart in the registry record, so the probe treats the PID as
        # live as long as it exists - this test process's own PID does.
        self._write_registry(_CWD, _UUID, os.getpid())

        self.assertFalse(delete_session(_UUID, _CWD))
        self.assertTrue(transcript.is_file())
        self.assertTrue(session_dir.is_dir())

    def test_deletes_transcript_and_subagent_folder(self) -> None:
        transcript, session_dir = self._write_session_files(_CWD, _UUID)

        self.assertTrue(delete_session(_UUID, _CWD))
        self.assertFalse(transcript.exists())
        self.assertFalse(session_dir.exists())

    def test_is_idempotent_when_nothing_is_on_disk(self) -> None:
        # A valid, non-live session with no files left (already deleted) is a
        # success, not an error.
        self.assertTrue(delete_session(_UUID, _CWD))

    def test_only_targets_the_named_session(self) -> None:
        target, target_dir = self._write_session_files(_CWD, _UUID)
        keep, keep_dir = self._write_session_files(_CWD, _OTHER_UUID)

        self.assertTrue(delete_session(_UUID, _CWD))

        self.assertFalse(target.exists())
        self.assertFalse(target_dir.exists())
        self.assertTrue(keep.is_file())
        self.assertTrue(keep_dir.is_dir())


class DeleteResolveErrorTest(unittest.TestCase):
    """A path resolve failure must degrade to a graceful False, never raise."""

    def test_within_returns_false_on_resolve_oserror(self) -> None:
        with mock.patch.object(Path, 'resolve', side_effect=OSError):
            self.assertFalse(_within(Path('C:/root'), Path('C:/root/x')))

    def test_delete_session_returns_false_on_projects_dir_resolve_oserror(self) -> None:
        with mock.patch('agent_monitor_for_claude.session_delete._is_live', return_value=False), \
             mock.patch('agent_monitor_for_claude.session_delete.projects_dir') as projects_dir:
            projects_dir.return_value.resolve.side_effect = OSError
            self.assertFalse(delete_session(_UUID, _CWD))


if __name__ == '__main__':
    unittest.main()
