"""Tests for the raw snapshot assembly (the data the UI derives from)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import snapshot as snapshot_mod
from agent_monitor_for_claude.paths import SessionRoot, transcript_path, windows_root
from agent_monitor_for_claude.snapshot import build_snapshot, live_or_recent_ids, registry_fingerprint

_END_TURN = json.dumps({
    'type': 'assistant',
    'timestamp': '2026-07-11T10:54:06Z',
    'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]},
})

_API_ERROR = json.dumps({
    'type': 'assistant',
    'timestamp': '2026-07-11T10:54:06Z',
    'isApiErrorMessage': True,
    'error': 'server_error',
    'apiErrorStatus': 529,
    'message': {'stop_reason': 'stop_sequence', 'model': '<synthetic>',
                'content': [{'type': 'text', 'text': 'API Error: 529 Overloaded.'}]},
})

_SUBAGENT_RUNNING = json.dumps({
    'type': 'assistant',
    'timestamp': '2026-07-11T10:54:06Z',
    'message': {'stop_reason': 'tool_use', 'content': [{'type': 'text', 'text': 'x'}]},
})

# The tail Claude Code writes for a local (`!` or slash) command: an injected
# isMeta "DO NOT respond" caveat, the command entry, and the system execution
# record. The model owes no reply, so the newest kind must read local_command.
_LOCAL_COMMAND_TAIL = '\n'.join([
    _END_TURN,
    json.dumps({'type': 'user', 'isMeta': True, 'timestamp': '2026-07-11T10:55:00Z',
                'message': {'role': 'user', 'content': '<local-command-caveat>x</local-command-caveat>'}}),
    json.dumps({'type': 'user', 'timestamp': '2026-07-11T10:55:00Z',
                'message': {'role': 'user', 'content': 'x'}}),
    json.dumps({'type': 'system', 'subtype': 'local_command', 'timestamp': '2026-07-11T10:55:00Z', 'content': 'x'}),
])

# An assistant turn followed only by an injected isMeta notice: the notice is
# not a turn, so the newest kind must stay the assistant turn (not user_text).
_META_AFTER_END_TURN = '\n'.join([
    _END_TURN,
    json.dumps({'type': 'user', 'isMeta': True, 'timestamp': '2026-07-11T10:55:00Z',
                'message': {'role': 'user', 'content': 'x'}}),
])


class _RegistryFixture(unittest.TestCase):
    """Isolated CLAUDE_CONFIG_DIR with a helper to register fake sessions.

    Session roots are pinned to just the Windows fixture root for every test
    in this class: without it, a real running WSL distro on the machine
    running the suite would be discovered and probed for real, making these
    Windows-only tests slow and dependent on that machine's WSL state. The
    multi-root tests below override this pin explicitly for their own scope.
    """

    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        (Path(self._temp.name) / 'sessions').mkdir()

        roots_patcher = mock.patch.object(snapshot_mod, 'session_roots', return_value=[windows_root()])
        roots_patcher.start()
        self.addCleanup(roots_patcher.stop)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _add_session(self, session_id: str, cwd: str) -> None:
        self._add_session_with_transcript(session_id, cwd, _END_TURN)

    def _add_session_with_transcript(self, session_id: str, cwd: str, transcript: str) -> None:
        pid = os.getpid()
        sessions = Path(self._temp.name) / 'sessions'
        (sessions / f'{session_id}.json').write_text(
            json.dumps({'pid': pid, 'sessionId': session_id, 'cwd': cwd, 'name': session_id, 'kind': 'interactive'}),
            encoding='utf-8',
        )
        path = transcript_path(windows_root(), session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript, encoding='utf-8')


class RawSnapshotTest(_RegistryFixture):
    def test_returns_flat_session_list(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        self._add_session('b', 'd:\\WebDev\\two')

        snapshot = build_snapshot()

        self.assertIn('generated_at', snapshot)
        self.assertEqual({session['session_id'] for session in snapshot['sessions']}, {'a', 'b'})

    def test_record_carries_raw_signals_without_derivation(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')

        session = next(s for s in build_snapshot()['sessions'] if s['session_id'] == 'a')

        self.assertTrue(session['alive'])
        self.assertTrue(session['has_transcript'])
        self.assertEqual(session['last_entry_kind'], 'assistant')
        self.assertEqual(session['last_stop_reason'], 'end_turn')
        # cwd is raw (lower-case drive) - display casing and grouping are the UI's job.
        self.assertEqual(session['cwd'], 'd:\\WebDev\\one')
        self.assertIn('usage', session)
        self.assertIn('age_seconds', session)
        # No derived fields leak in from the old formatting layer.
        self.assertNotIn('status', session)
        self.assertNotIn('status_label', session)

    def test_record_carries_the_api_error_fields(self) -> None:
        # All three fields an errored session's status is built from have to reach
        # the record. The parser and the UI are each tested on their own, so a
        # field dropped in between them would surface only in the window.
        self._add_session_with_transcript('e', 'd:\\WebDev\\one', _API_ERROR)

        session = next(s for s in build_snapshot()['sessions'] if s['session_id'] == 'e')

        self.assertEqual(session['last_entry_kind'], 'api_error')
        self.assertEqual(session['api_error_kind'], 'server_error')
        self.assertEqual(session['api_error_status'], 529)
        self.assertEqual(session['api_error_detail'], 'API Error: 529 Overloaded.')

    def test_record_carries_native_status_and_waiting_for(self) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        (sessions / 'w.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': 'w', 'cwd': 'd:\\x',
                        'kind': 'interactive', 'status': 'waiting', 'waitingFor': 'permission prompt'}),
            encoding='utf-8',
        )
        path = transcript_path(windows_root(), 'w', 'd:\\x')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_END_TURN, encoding='utf-8')

        session = next(s for s in build_snapshot()['sessions'] if s['session_id'] == 'w')

        self.assertEqual(session['native_status'], 'waiting')
        self.assertEqual(session['waiting_for'], 'permission prompt')

    def test_local_command_tail_is_its_own_kind(self) -> None:
        self._add_session_with_transcript('lc', 'd:\\WebDev\\lc', _LOCAL_COMMAND_TAIL)

        session = next(s for s in build_snapshot()['sessions'] if s['session_id'] == 'lc')

        self.assertEqual(session['last_entry_kind'], 'local_command')

    def test_meta_notice_does_not_drive_state(self) -> None:
        self._add_session_with_transcript('m', 'd:\\WebDev\\m', _META_AFTER_END_TURN)

        session = next(s for s in build_snapshot()['sessions'] if s['session_id'] == 'm')

        # The isMeta notice is skipped, so the finished assistant turn stands.
        self.assertEqual(session['last_entry_kind'], 'assistant')


class EndedRetentionTest(_RegistryFixture):
    """The window an ended session stays visible for is measured from when it ended.

    ``last_seen`` supplies that moment (the last poll that saw the process
    alive).  Keying the window on the transcript's activity age instead - the
    previous rule, still the fallback - drops a session that sat idle for hours
    and was then closed in the very same poll that noticed it ended, which is
    exactly the session someone who closed a window by accident is after.
    """

    # Last activity far past any retention window, so only a sighting can keep
    # this session in the overview.
    _STALE_TURN = json.dumps({
        'type': 'assistant',
        'timestamp': '2020-01-01T10:00:00Z',
        'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]},
    })

    def setUp(self) -> None:
        super().setUp()
        # The sighting memory is process-global, so clear it around each test -
        # a leftover sighting from another test would decide this one's outcome.
        snapshot_mod.prune_last_seen([])
        self.addCleanup(snapshot_mod.prune_last_seen, [])

    def _add_ended_session(self, session_id: str) -> None:
        # A live pid with a mismatched procStart reads as a recycled pid, so the
        # record is present but not alive - a session that has ended.
        sessions = Path(self._temp.name) / 'sessions'
        (sessions / f'{session_id}.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': session_id, 'cwd': 'd:\\x',
                        'kind': 'interactive', 'procStart': '1'}),
            encoding='utf-8',
        )
        path = transcript_path(windows_root(), session_id, 'd:\\x')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._STALE_TURN, encoding='utf-8')

    def _ids(self) -> set[str]:
        return {session['session_id'] for session in build_snapshot()['sessions']}

    def test_recently_ended_session_is_retained_despite_stale_activity(self) -> None:
        self._add_ended_session('closed')

        with mock.patch.object(snapshot_mod, 'seconds_since_alive', return_value=60.0):
            self.assertIn('closed', self._ids())

    def test_session_ended_past_the_window_is_dropped(self) -> None:
        self._add_ended_session('closed')

        with mock.patch.object(snapshot_mod, 'seconds_since_alive', return_value=snapshot_mod.ENDED_MAX_AGE + 1):
            self.assertNotIn('closed', self._ids())

    def test_without_a_sighting_the_activity_age_still_decides(self) -> None:
        # Already dead when the monitor started: no sighting exists, so the
        # previous rule (activity age) applies unchanged.
        self._add_ended_session('closed')

        with mock.patch.object(snapshot_mod, 'seconds_since_alive', return_value=None):
            self.assertNotIn('closed', self._ids())

    def test_a_live_session_records_a_sighting(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')

        build_snapshot()

        self.assertIsNotNone(snapshot_mod.seconds_since_alive('windows', 'a'))

    def test_sightings_are_pruned_with_the_registry(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        build_snapshot()

        (Path(self._temp.name) / 'sessions' / 'a.json').unlink()
        build_snapshot()

        self.assertIsNone(snapshot_mod.seconds_since_alive('windows', 'a'))


class PerRecordIsolationTest(_RegistryFixture):
    def test_one_failing_record_does_not_blank_the_snapshot(self) -> None:
        # An unforeseen failure while assembling one session must skip that
        # record only - the rest of the overview must still be returned.
        self._add_session('a', 'd:\\WebDev\\one')
        self._add_session('b', 'd:\\WebDev\\two')

        real_state_for = snapshot_mod.state_for

        def flaky(root, session_id: str, cwd: str):
            if session_id == 'a':
                raise RuntimeError('boom')
            return real_state_for(root, session_id, cwd)

        with mock.patch.object(snapshot_mod, 'state_for', side_effect=flaky):
            ids = {session['session_id'] for session in build_snapshot()['sessions']}

        self.assertNotIn('a', ids)
        self.assertIn('b', ids)


class NewSessionAgeTest(_RegistryFixture):
    def test_new_session_age_falls_back_to_started_at(self) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        started_ms = (time.time() - 90) * 1000
        (sessions / 'n.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': 'nn', 'cwd': 'd:\\x', 'startedAt': started_ms}),
            encoding='utf-8',
        )

        session = build_snapshot()['sessions'][0]

        self.assertFalse(session['has_transcript'])
        self.assertTrue(80 <= session['age_seconds'] <= 100)


class SubagentRawTest(_RegistryFixture):
    def test_running_subagent_counted(self) -> None:
        self._add_session('s', 'd:\\WebDev\\proj')

        subagents = transcript_path(windows_root(), 's', 'd:\\WebDev\\proj').parent / 's' / 'subagents'
        subagents.mkdir(parents=True, exist_ok=True)
        (subagents / 'agent-1.jsonl').write_text(_SUBAGENT_RUNNING, encoding='utf-8')

        session = build_snapshot()['sessions'][0]

        self.assertEqual(session['subagents_running'], 1)


class FingerprintTest(_RegistryFixture):
    def test_stable_without_changes(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        self.assertEqual(registry_fingerprint(), registry_fingerprint())

    def test_changes_when_transcript_grows(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        before = registry_fingerprint()

        path = transcript_path(windows_root(), 'a', 'd:\\WebDev\\one')
        with path.open('a', encoding='utf-8') as handle:
            handle.write('\n' + _END_TURN)

        self.assertNotEqual(before, registry_fingerprint())

    def test_changes_when_session_appears(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        before = registry_fingerprint()

        self._add_session('b', 'd:\\WebDev\\two')

        self.assertNotEqual(before, registry_fingerprint())

    def test_changes_when_waiting_for_changes(self) -> None:
        self._add_session('a', 'd:\\WebDev\\one')
        registry = Path(self._temp.name) / 'sessions' / 'a.json'
        base = {'pid': os.getpid(), 'sessionId': 'a', 'cwd': 'd:\\WebDev\\one', 'kind': 'interactive', 'status': 'waiting'}

        registry.write_text(json.dumps({**base, 'waitingFor': 'permission prompt'}), encoding='utf-8')
        before = registry_fingerprint()

        registry.write_text(json.dumps({**base, 'waitingFor': 'plan review'}), encoding='utf-8')

        self.assertNotEqual(before, registry_fingerprint())


def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int) -> None:
    """Write a synthetic ``/proc/<pid>/stat`` entry (mirrors ``tests/test_wsl.py``'s helper)."""
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields3plus = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
                   '50', '10', '0', '0', '20', '0', '4', '0', str(starttime), '1000000', '500']
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields3plus), encoding='utf-8')


class MultiRootSnapshotTest(_RegistryFixture):
    """Snapshot assembly across the Windows fixture root and a fake WSL root together.

    The WSL side gets its own temp dir standing in for a distro's ``.claude``
    (``config_dir``) and ``/proc`` (``proc_dir``), built the same way
    ``tests/test_wsl.py`` builds one for ``probe_wsl_sessions`` directly - here
    it runs through the full ``build_snapshot``/``registry_fingerprint``/
    ``live_or_recent_ids`` pipeline instead, with ``session_roots`` patched
    (overriding the single-root pin from ``_RegistryFixture.setUp``) to return
    both roots together.
    """

    _WSL_PID = 100
    _WSL_STARTTIME = 5000
    _WSL_SESSION_ID = 'wsl-sid'
    _WSL_CWD = '/home/dev/proj'

    def setUp(self) -> None:
        super().setUp()
        wsl_temp = tempfile.TemporaryDirectory()
        self.addCleanup(wsl_temp.cleanup)
        self._wsl_root = SessionRoot(
            origin='wsl:U', label='U',
            config_dir=Path(wsl_temp.name) / 'cfg',
            proc_dir=Path(wsl_temp.name) / 'proc',
            temp_dir=Path(wsl_temp.name) / 'tmp',
        )
        (self._wsl_root.config_dir / 'sessions').mkdir(parents=True)

    def _roots(self) -> list[SessionRoot]:
        return [windows_root(), self._wsl_root]

    def _add_wsl_session(self, session_id: str, cwd: str, pid: int, transcript: str = _END_TURN) -> None:
        # procStart here is Linux ticks-since-boot (matching /proc/<pid>/stat
        # field 22, see wsl.probe_wsl_sessions), not the .NET wall-clock ticks
        # a native Windows registry record carries under the same field name -
        # each root's own prober is what gives the raw integer its meaning.
        sessions = self._wsl_root.config_dir / 'sessions'
        (sessions / f'{session_id}.json').write_text(
            json.dumps({'pid': pid, 'sessionId': session_id, 'cwd': cwd, 'name': session_id, 'kind': 'interactive',
                        'procStart': self._WSL_STARTTIME}),
            encoding='utf-8',
        )
        path = transcript_path(self._wsl_root, session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript, encoding='utf-8')
        _write_stat(self._wsl_root.proc_dir, pid, 'claude', 1, self._WSL_STARTTIME)

    def test_wsl_session_appears_with_origin_and_populated_transcript_state(self) -> None:
        self._add_wsl_session(self._WSL_SESSION_ID, self._WSL_CWD, self._WSL_PID)

        with mock.patch.object(snapshot_mod, 'session_roots', return_value=self._roots()):
            snapshot = build_snapshot()

        session = next(s for s in snapshot['sessions'] if s['session_id'] == self._WSL_SESSION_ID)
        self.assertEqual(session['origin'], 'wsl:U')
        self.assertEqual(session['origin_label'], 'U')
        self.assertTrue(session['alive'])
        self.assertTrue(session['has_transcript'])
        self.assertEqual(session['last_entry_kind'], 'assistant')
        self.assertEqual(session['last_stop_reason'], 'end_turn')
        self.assertIsNotNone(session['age_seconds'])

    def test_pid_collision_between_windows_and_wsl_roots_is_independent(self) -> None:
        self._add_wsl_session(self._WSL_SESSION_ID, self._WSL_CWD, self._WSL_PID)

        # A Windows-root record with the SAME raw pid number, forced not-alive
        # via a deliberately mismatched procStart (the recycled-pid guard in
        # process_probe) so the assertion never depends on whether pid 100
        # happens to be a real live process on the machine running this test -
        # only on whether the two roots' probes stay independent of each other.
        sessions = Path(self._temp.name) / 'sessions'
        (sessions / 'win-sid.json').write_text(
            json.dumps({'pid': self._WSL_PID, 'sessionId': 'win-sid', 'cwd': 'd:\\WebDev\\collide',
                        'kind': 'interactive', 'procStart': '1'}),
            encoding='utf-8',
        )

        with mock.patch.object(snapshot_mod, 'session_roots', return_value=self._roots()):
            snapshot = build_snapshot()

        sessions_by_id = {s['session_id']: s for s in snapshot['sessions']}
        self.assertTrue(sessions_by_id[self._WSL_SESSION_ID]['alive'])
        # The Windows record either reads not-alive or is dropped entirely (it
        # has no transcript, so it does not qualify for ended-session
        # retention) - either way it must never inherit the WSL pid's liveness.
        if 'win-sid' in sessions_by_id:
            self.assertFalse(sessions_by_id['win-sid']['alive'])

    def test_fingerprint_includes_both_origins_and_reacts_to_wsl_transcript_growth(self) -> None:
        self._add_wsl_session(self._WSL_SESSION_ID, self._WSL_CWD, self._WSL_PID)
        self._add_session('win-sid', 'd:\\WebDev\\two')

        with mock.patch.object(snapshot_mod, 'session_roots', return_value=self._roots()):
            before = registry_fingerprint()

            parts = before.split('|')
            self.assertTrue(any(part.startswith('windows:') for part in parts))
            self.assertTrue(any(part.startswith('wsl:U:') for part in parts))

            path = transcript_path(self._wsl_root, self._WSL_SESSION_ID, self._WSL_CWD)
            with path.open('a', encoding='utf-8') as handle:
                handle.write('\n' + _END_TURN)

            after = registry_fingerprint()

        self.assertNotEqual(before, after)

    def test_live_or_recent_ids_includes_the_live_wsl_session(self) -> None:
        self._add_wsl_session(self._WSL_SESSION_ID, self._WSL_CWD, self._WSL_PID)

        with mock.patch.object(snapshot_mod, 'session_roots', return_value=self._roots()):
            ids = live_or_recent_ids()

        self.assertIn(self._WSL_SESSION_ID, ids)


if __name__ == '__main__':
    unittest.main()
