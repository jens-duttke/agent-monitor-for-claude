"""
Tests for session deletion - the application's only sanctioned write surface.

``session_delete.delete_session`` removes a past session's transcript and
subagent folder.  These tests cover the three guards (UUID validation, the
live-process refusal, path confinement), the actual removal, idempotency, and
(in ``DeleteOriginTest``) that every guard and the deletion itself resolve
against the caller-supplied origin's own root, checking liveness across every
currently available root rather than only the Windows one.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import session_delete
from agent_monitor_for_claude.paths import SessionRoot, cwd_to_slug, windows_root
from agent_monitor_for_claude.session_delete import delete_session, _within

_CWD = 'd:\\PythonDev\\demo-proj'
_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
_OTHER_UUID = '11111111-2222-3333-4444-555555555555'

# A syntactically valid session id, standing in for a real Claude Code session
# uuid on the fake WSL root DeleteOriginTest builds.
WSL_SID = '8d49a52c-4ac7-43ec-a7e1-773be955bf59'


class DeleteEnvTest(unittest.TestCase):
    """Isolated CLAUDE_CONFIG_DIR, with session roots pinned to the Windows fixture root.

    Mirrors ``test_snapshot.py``'s ``_RegistryFixture`` / ``test_history.py``'s
    ``HistoryEnvTest`` / ``test_search.py``'s ``SearchEnvTest``: without this pin,
    ``delete_session``'s origin resolution (``root_for_origin``) and its root-wide
    liveness walk (``_is_live``, via ``session_roots``) would reach the real
    ``roots.session_roots()`` - and a real running WSL distro on the machine
    running the suite - on every test below, including every pre-existing one
    (none of them pass an ``origin``, so they all resolve the default
    ``'windows'``).  ``DeleteOriginTest`` overrides both pins explicitly for its
    own narrower scope.
    """

    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name

        self.windows_root = windows_root()
        origin_patcher = mock.patch.object(
            session_delete, 'root_for_origin',
            side_effect=lambda origin: self.windows_root if origin == 'windows' else None,
        )
        origin_patcher.start()
        self.addCleanup(origin_patcher.stop)

        roots_patcher = mock.patch.object(session_delete, 'session_roots', return_value=[self.windows_root])
        roots_patcher.start()
        self.addCleanup(roots_patcher.stop)

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


def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int) -> None:
    """Write a minimal ``/proc/[pid]/stat`` file, mirroring ``tests/test_wsl.py``'s helper."""
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields3plus = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
                   '50', '10', '0', '0', '20', '0', '4', '0', str(starttime), '1000000', '500']
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields3plus), encoding='utf-8')


class DeleteOriginTest(DeleteEnvTest):
    """``delete_session`` resolves its root from the caller-supplied origin, not just Windows.

    ``_is_live`` becomes root-wide: a session id is checked against every
    currently available root, each probed through its own kind
    (``process_probe.probe_all`` for Windows, ``wsl.probe_wsl_sessions`` for
    WSL) - see ``session_delete._is_live``.
    """

    def _wsl_root_with_session(self, base: str, alive_pid: int | None) -> SessionRoot:
        root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                            proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')
        project = root.config_dir / 'projects' / '-home-dev-proj'
        (project / WSL_SID).mkdir(parents=True)                      # subagent dir
        (project / f'{WSL_SID}.jsonl').write_text('{}\n', encoding='utf-8')
        root.proc_dir.mkdir(parents=True)
        if alive_pid is not None:
            _write_stat(root.proc_dir, alive_pid, 'claude', 1, 5000)
            registry = root.config_dir / 'sessions'
            registry.mkdir(parents=True)
            (registry / f'{alive_pid}.json').write_text(
                json.dumps({'pid': alive_pid, 'sessionId': WSL_SID, 'cwd': '/home/dev/proj', 'procStart': '5000'}),
                encoding='utf-8')
        return root

    def test_deletes_dead_wsl_session(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_session(base, alive_pid=None)
            with mock.patch.object(session_delete, 'root_for_origin', return_value=root), \
                 mock.patch.object(session_delete, 'session_roots', return_value=[root]):
                self.assertTrue(delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))
            self.assertFalse((root.config_dir / 'projects' / '-home-dev-proj' / f'{WSL_SID}.jsonl').exists())
            self.assertFalse((root.config_dir / 'projects' / '-home-dev-proj' / WSL_SID).exists())

    def test_refuses_live_wsl_session(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_session(base, alive_pid=321)
            with mock.patch.object(session_delete, 'root_for_origin', return_value=root), \
                 mock.patch.object(session_delete, 'session_roots', return_value=[root]):
                self.assertFalse(delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))
            self.assertTrue((root.config_dir / 'projects' / '-home-dev-proj' / f'{WSL_SID}.jsonl').exists())

    def test_refuses_unknown_origin(self) -> None:
        with mock.patch.object(session_delete, 'root_for_origin', return_value=None):
            self.assertFalse(delete_session(WSL_SID, '/home/dev/proj', 'gone:X'))

    def test_treats_a_naming_roots_probe_error_as_live(self) -> None:
        # A deletion guard must fail SAFE: a root whose registry names the
        # session id but whose probe then raises (a dropped WSL mount, say)
        # must refuse deletion, not silently read as dead just because the
        # probe itself failed.
        with tempfile.TemporaryDirectory() as base:
            root = self._wsl_root_with_session(base, alive_pid=321)
            with mock.patch.object(session_delete, 'root_for_origin', return_value=root), \
                 mock.patch.object(session_delete, 'session_roots', return_value=[root]), \
                 mock.patch.object(session_delete, 'probe_wsl_sessions', side_effect=OSError):
                self.assertFalse(delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))
            self.assertTrue((root.config_dir / 'projects' / '-home-dev-proj' / f'{WSL_SID}.jsonl').exists())

    def test_other_roots_with_unrelated_sessions_do_not_block_deletion(self) -> None:
        # The Windows root carries a live session under a different id; the WSL
        # root carries the dead target.  Every root is walked, but only a root
        # that actually names the target id can refuse the deletion - an
        # unrelated live session elsewhere must not.
        self._write_registry(_CWD, _UUID, os.getpid())
        with tempfile.TemporaryDirectory() as base:
            wsl_root = self._wsl_root_with_session(base, alive_pid=None)
            with mock.patch.object(session_delete, 'root_for_origin', return_value=wsl_root), \
                 mock.patch.object(session_delete, 'session_roots', return_value=[self.windows_root, wsl_root]):
                self.assertTrue(delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))


class DeleteResolveErrorTest(DeleteEnvTest):
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
