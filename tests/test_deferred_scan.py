"""The first full scan of a large transcript runs in the background, and what a snapshot shows meanwhile."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest import mock

from agent_monitor_for_claude import snapshot, transcript
from agent_monitor_for_claude.paths import local_root, transcript_path

_SESSION_ID = 'deferred-session-id'
_CWD = 'c:\\Temp\\deferred-proj'


def _assistant(tokens: int, timestamp: str = '2026-07-11T10:00:00Z', model: str = 'claude-opus-4-8') -> str:
    return json.dumps({
        'type': 'assistant', 'timestamp': timestamp, 'version': '2.1.250',
        'message': {'stop_reason': 'end_turn', 'model': model, 'usage': {'input_tokens': tokens, 'output_tokens': 0},
                    'content': [{'type': 'text', 'text': 'reply'}]},
    })


def _prompt(text: str, mode: str | None = None, sidechain: bool = False) -> str:
    entry: dict = {'type': 'user', 'timestamp': '2026-07-11T09:59:00Z', 'message': {'content': text}}
    if mode is not None:
        entry['permissionMode'] = mode
    if sidechain:
        entry['isSidechain'] = True
    return json.dumps(entry)


def _finish_background_scans() -> None:
    worker = transcript._deferred.worker
    if worker is not None:
        worker.join(timeout=10)


class DeferredScanEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        transcript._scan_cache.clear()

    def tearDown(self) -> None:
        _finish_background_scans()
        with transcript._deferred.lock:
            transcript._deferred.pending.clear()
        transcript._scan_cache.clear()
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _write_transcript(self, lines: list[str]) -> Path:
        path = transcript_path(local_root(), _SESSION_ID, _CWD)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: text mode would write '\r\n' on Windows, and the
        # byte-level tests below count exact offsets.
        path.write_bytes(('\n'.join(lines) + '\n').encode('utf-8'))
        return path

    def _defer_everything(self) -> AbstractContextManager[Any]:
        """Make every transcript count as large, so its first scan is deferred."""
        return mock.patch.object(transcript, '_SYNC_SCAN_BYTES', 0)


class DeferredStateTest(DeferredScanEnvTest):
    def test_small_transcript_is_scanned_on_the_spot(self) -> None:
        self._write_transcript([_assistant(100)])

        state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertFalse(state.scan_pending)
        self.assertEqual(state.usage['input_tokens'], 100)

    def test_large_transcript_is_pending_until_its_background_scan_lands(self) -> None:
        self._write_transcript([
            _prompt('First question', mode='default'),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Generated title'}),
            _assistant(100),
            _assistant(50, timestamp='2026-07-11T10:01:00Z'),
        ])
        completed = transcript.completed_background_scans()

        with self._defer_everything():
            pending = transcript.state_for(local_root(), _SESSION_ID, _CWD)
            _finish_background_scans()
            done = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        # Pending: the tail's state is all there, the full scan's totals are
        # unknown rather than zero, and title and mode come from near the end.
        self.assertTrue(pending.scan_pending)
        self.assertEqual(pending.last_entry_kind, 'assistant')
        self.assertIsNone(pending.usage)
        self.assertIsNone(pending.model_timeline)
        self.assertEqual(pending.title, 'Generated title')
        self.assertEqual(pending.permission_mode, 'default')

        self.assertFalse(done.scan_pending)
        self.assertEqual(done.usage['input_tokens'], 150)
        self.assertEqual(done.title, 'Generated title')
        self.assertEqual(transcript.completed_background_scans(), completed + 1)

    def test_scanned_transcript_grows_incrementally_without_deferring_again(self) -> None:
        path = self._write_transcript([_assistant(100)])

        with self._defer_everything():
            transcript.state_for(local_root(), _SESSION_ID, _CWD)
            _finish_background_scans()
            with path.open('a', encoding='utf-8') as handle:
                handle.write(_assistant(7) + '\n')
            state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertFalse(state.scan_pending)
        self.assertEqual(state.usage['input_tokens'], 107)

    def test_pending_title_prefers_a_rename_over_a_later_generated_title(self) -> None:
        self._write_transcript([
            json.dumps({'type': 'custom-title', 'customTitle': 'Renamed'}),
            json.dumps({'type': 'ai-title', 'aiTitle': 'Generated'}),
            _assistant(1),
        ])

        with self._defer_everything():
            state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertTrue(state.scan_pending)
        self.assertEqual(state.title, 'Renamed')

    def test_pending_state_never_takes_a_prompt_as_title(self) -> None:
        # The oldest prompt inside the look-back window is not the session's
        # first, so a session named only by its prompt shows no title until the
        # full scan has found the real one.
        self._write_transcript([_prompt('Some prompt'), _assistant(1)])

        with self._defer_everything():
            pending = transcript.state_for(local_root(), _SESSION_ID, _CWD)
            _finish_background_scans()
            done = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertIsNone(pending.title)
        self.assertEqual(done.title, 'Some prompt')

    def test_pending_mode_skips_sidechain_entries_like_the_full_scan(self) -> None:
        self._write_transcript([
            _prompt('Main', mode='acceptEdits'),
            _prompt('Subagent', mode='bypassPermissions', sidechain=True),
            _assistant(1),
        ])

        with self._defer_everything():
            state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertEqual(state.permission_mode, 'acceptEdits')

    def test_pending_look_back_is_bounded(self) -> None:
        # A mode written further back than the window is left to the full scan.
        self._write_transcript([_prompt('Main', mode='default')] + [_assistant(1)] * 20)

        with self._defer_everything(), mock.patch.object(transcript, '_PENDING_LOOKBACK_BYTES', 400):
            state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertTrue(state.scan_pending)
        self.assertIsNone(state.permission_mode)

    def test_snapshot_marks_the_pending_record_and_its_fingerprint_moves_on_completion(self) -> None:
        sessions = Path(self._temp.name) / 'sessions'
        sessions.mkdir()
        (sessions / f'{os.getpid()}.json').write_text(
            json.dumps({'pid': os.getpid(), 'sessionId': _SESSION_ID, 'cwd': _CWD, 'name': 'n', 'kind': 'interactive'}),
            encoding='utf-8',
        )
        self._write_transcript([_assistant(100)])

        with self._defer_everything():
            # Holding the scans keeps the background thread from finishing
            # between the snapshot and the first fingerprint.
            with transcript.pause_background_scans():
                pending = snapshot.build_snapshot()['sessions']
                before = snapshot.registry_fingerprint()
            _finish_background_scans()
            after = snapshot.registry_fingerprint()
            done = snapshot.build_snapshot()['sessions']

        self.assertEqual([record['scan_pending'] for record in pending], [True])
        self.assertEqual([record['scan_pending'] for record in done], [False])
        self.assertEqual(done[0]['usage']['input_tokens'], 100)
        self.assertNotEqual(before, after)


class ChunkedReadTest(DeferredScanEnvTest):
    def test_lines_split_across_chunks_are_absorbed_once_and_whole(self) -> None:
        path = self._write_transcript([_assistant(tokens) for tokens in (1, 20, 300)])

        with mock.patch.object(transcript, '_SCAN_CHUNK_BYTES', 7):
            result = transcript._scan_appended(path)

        self.assertEqual(result.usage['input_tokens'], 321)

    def test_a_partial_last_line_is_reported_but_read_again_later(self) -> None:
        path = transcript_path(local_root(), _SESSION_ID, _CWD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_assistant(1) + '\n' + _assistant(20), encoding='utf-8')

        with mock.patch.object(transcript, '_SCAN_CHUNK_BYTES', 7):
            first = transcript._scan_appended(path)
            with path.open('a', encoding='utf-8') as handle:
                handle.write('\n' + _assistant(300) + '\n')
            second = transcript._scan_appended(path)

        self.assertEqual(first.usage['input_tokens'], 21)
        self.assertEqual(second.usage['input_tokens'], 321)

    def test_lines_newest_first_rejoins_lines_across_chunks(self) -> None:
        lines = [f'line-{index}-' + 'x' * index for index in range(12)]
        path = self._write_transcript(lines)

        with mock.patch.object(transcript, '_LOOKBACK_CHUNK_BYTES', 5):
            read = [line.decode() for line in transcript._lines_newest_first(path, 10_000)]

        self.assertEqual([line for line in read if line], list(reversed(lines)))

    def test_lines_newest_first_drops_the_line_cut_by_the_window(self) -> None:
        path = self._write_transcript(['aaaaaaaaaa', 'bbbb', 'cccc'])

        read = [line.decode() for line in transcript._lines_newest_first(path, 12)]

        self.assertEqual([line for line in read if line], ['cccc', 'bbbb'])


class PauseTest(DeferredScanEnvTest):
    def test_background_scan_waits_while_a_reader_is_inside(self) -> None:
        waiter = threading.Thread(target=transcript._wait_for_readers, daemon=True)

        with transcript.pause_background_scans():
            waiter.start()
            waiter.join(timeout=0.05)
            self.assertTrue(waiter.is_alive())

        waiter.join(timeout=5)
        self.assertFalse(waiter.is_alive())

    def test_a_failing_scan_does_not_stop_the_queue_behind_it(self) -> None:
        path = self._write_transcript([_assistant(100)])
        missing = path.with_name('missing.jsonl')

        completed = transcript.completed_background_scans()
        with transcript._deferred.lock:
            # Queued directly, smallest first: the missing file is tried first.
            transcript._deferred.pending['missing'] = (0, missing)
        with self._defer_everything():
            transcript.state_for(local_root(), _SESSION_ID, _CWD)
            _finish_background_scans()
            state = transcript.state_for(local_root(), _SESSION_ID, _CWD)

        self.assertFalse(state.scan_pending)
        self.assertEqual(state.usage['input_tokens'], 100)
        # Only the scan that stored a result counts, or a file that keeps
        # failing would move the fingerprint - and trigger a snapshot - forever.
        self.assertEqual(transcript.completed_background_scans(), completed + 1)

    def test_a_state_stored_meanwhile_is_not_overwritten(self) -> None:
        # worktree_for scans synchronously; its state has moved on from where a
        # fresh background scan would start.
        path = self._write_transcript([_assistant(100)])
        cache_key = os.path.normcase(str(path))
        existing = transcript._ScanState()
        transcript._scan_cache[cache_key] = existing

        self.assertFalse(transcript._first_scan(cache_key, path))
        self.assertIs(transcript._scan_cache[cache_key], existing)

    def test_prune_drops_queued_scans_of_sessions_that_left_the_registry(self) -> None:
        path = self._write_transcript([_assistant(100)])
        with transcript._deferred.lock:
            transcript._deferred.pending['gone'] = (1, path)
            transcript._deferred.pending[os.path.normcase(str(path))] = (1, path)

        transcript.prune_scan_cache([(local_root(), _SESSION_ID, _CWD)])

        with transcript._deferred.lock:
            self.assertEqual(list(transcript._deferred.pending), [os.path.normcase(str(path))])


if __name__ == '__main__':
    unittest.main()
