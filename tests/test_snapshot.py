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
from agent_monitor_for_claude.paths import transcript_path
from agent_monitor_for_claude.snapshot import build_snapshot, registry_fingerprint

_END_TURN = json.dumps({
    'type': 'assistant',
    'timestamp': '2026-07-11T10:54:06Z',
    'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]},
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
    """Isolated CLAUDE_CONFIG_DIR with a helper to register fake sessions."""

    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        (Path(self._temp.name) / 'sessions').mkdir()

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
        path = transcript_path(session_id, cwd)
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

    def test_record_carries_native_status_and_waiting_for(self) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        (sessions / 'w.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': 'w', 'cwd': 'd:\\x',
                        'kind': 'interactive', 'status': 'waiting', 'waitingFor': 'permission prompt'}),
            encoding='utf-8',
        )
        path = transcript_path('w', 'd:\\x')
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


class PerRecordIsolationTest(_RegistryFixture):
    def test_one_failing_record_does_not_blank_the_snapshot(self) -> None:
        # An unforeseen failure while assembling one session must skip that
        # record only - the rest of the overview must still be returned.
        self._add_session('a', 'd:\\WebDev\\one')
        self._add_session('b', 'd:\\WebDev\\two')

        real_state_for = snapshot_mod.state_for

        def flaky(session_id: str, cwd: str):
            if session_id == 'a':
                raise RuntimeError('boom')
            return real_state_for(session_id, cwd)

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

        subagents = transcript_path('s', 'd:\\WebDev\\proj').parent / 's' / 'subagents'
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

        path = transcript_path('a', 'd:\\WebDev\\one')
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


if __name__ == '__main__':
    unittest.main()
