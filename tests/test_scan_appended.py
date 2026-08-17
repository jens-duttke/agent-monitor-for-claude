"""Concurrency guard for the incremental usage scan (no double-counting)."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript
from agent_monitor_for_claude.paths import SessionRoot, cwd_to_slug, transcript_path, windows_root

_TURN = '{"type":"assistant","message":{"model":"claude-opus-4-8","usage":{"input_tokens":100,"output_tokens":0}}}\n'


class ScanAppendedConcurrencyTest(unittest.TestCase):
    """Two overlapping snapshot builds must not sum an appended turn twice.

    pywebview dispatches each ``js_api`` call on its own thread, so two
    ``get_snapshot`` calls can run ``_scan_appended`` for the same path at once.
    Both fetch the same cached ``_ScanState``, and if both read the delta before
    either commits ``consumed``, the appended bytes get absorbed into the shared
    state twice - permanently inflating the session's token total and cost.
    """

    def setUp(self) -> None:
        transcript._scan_cache.clear()

    def tearDown(self) -> None:
        transcript._scan_cache.clear()

    def test_concurrent_delta_scan_does_not_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'

            # Prime the cache with one turn so both threads later share one state.
            path.write_text(_TURN, encoding='utf-8')
            self.assertEqual(transcript._scan_appended(path).usage['input_tokens'], 100)

            # Append a second turn: this delta is what a race would double-count.
            with path.open('a', encoding='utf-8') as handle:
                handle.write(_TURN)

            # Force both threads to read the appended bytes before either mutates
            # the shared state - the exact interleaving the guard must prevent.
            barrier = threading.Barrier(2, timeout=0.5)
            synced: set[int] = set()
            sync_lock = threading.Lock()
            real_absorb = transcript._absorb_line

            def gated_absorb(line, state):
                tid = threading.get_ident()
                with sync_lock:
                    first = tid not in synced
                    if first:
                        synced.add(tid)
                if first:
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                return real_absorb(line, state)

            errors: list[Exception] = []

            def worker() -> None:
                try:
                    transcript._scan_appended(path)
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(transcript, '_absorb_line', gated_absorb):
                threads = [threading.Thread(target=worker) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertEqual(errors, [])

            # Two turns of 100 input tokens = 200; a double-counted delta gives 300.
            self.assertEqual(transcript._scan_appended(path).usage['input_tokens'], 200)


class TimelineMemoizationTest(unittest.TestCase):
    """A timeline is rebuilt only when new events arrived, and never handed out shared.

    ``_scan_result`` runs on every poll, while a transcript has usually not grown
    since the last one.  Building a timeline sorts every event and parses each
    timestamp for the sort key, so rebuilding it per poll costs tens of
    milliseconds for a session with thousands of turns - per session, every
    second.  Events are only ever appended, so the event count decides.
    """

    def setUp(self) -> None:
        transcript._scan_cache.clear()

    def tearDown(self) -> None:
        transcript._scan_cache.clear()

    @staticmethod
    def _turn(timestamp: str, model: str, version: str) -> str:
        return (
            '{"type":"assistant","timestamp":"' + timestamp + '","version":"' + version + '",'
            '"message":{"model":"' + model + '","usage":{"input_tokens":1,"output_tokens":0}}}\n'
        )

    def test_unchanged_file_reuses_the_timelines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(self._turn('2026-07-11T09:00:00Z', 'claude-opus-4-8', '2.1.224'), encoding='utf-8')

            first = transcript._scan_appended(path)

            with mock.patch.object(transcript, '_run_timeline', side_effect=AssertionError('rebuilt')) as never:
                second = transcript._scan_appended(path)

            never.assert_not_called()
            self.assertEqual(second.model_timeline, first.model_timeline)
            self.assertEqual(second.cli_timeline, first.cli_timeline)

    def test_appended_turn_invalidates_the_timelines(self) -> None:
        # The stale-cache case: a switch in the appended bytes must show up, not
        # be hidden behind a timeline built before it existed.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(self._turn('2026-07-11T09:00:00Z', 'claude-opus-4-8', '2.1.224'), encoding='utf-8')
            transcript._scan_appended(path)

            with path.open('a', encoding='utf-8') as handle:
                handle.write(self._turn('2026-07-11T11:00:00Z', 'claude-sonnet-5', '2.1.228'))

            result = transcript._scan_appended(path)

            self.assertEqual([entry['model'] for entry in result.model_timeline], ['claude-opus-4-8', 'claude-sonnet-5'])
            self.assertEqual([entry['version'] for entry in result.cli_timeline], ['2.1.224', '2.1.228'])

    def test_returned_timeline_is_not_the_cached_one(self) -> None:
        # The result crosses into the snapshot, so a caller mutating it must not
        # corrupt what the next poll reports.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(self._turn('2026-07-11T09:00:00Z', 'claude-opus-4-8', '2.1.224'), encoding='utf-8')

            first = transcript._scan_appended(path)
            first.model_timeline.clear()
            first.cli_timeline[0]['version'] = 'tampered'

            second = transcript._scan_appended(path)

            self.assertEqual([entry['model'] for entry in second.model_timeline], ['claude-opus-4-8'])
            self.assertEqual([entry['version'] for entry in second.cli_timeline], ['2.1.224'])


class PermissionModeScanTest(unittest.TestCase):
    """A mode switch arrives as appended bytes, not in the priming scan.

    Only the first poll reads a whole transcript; every later one parses just
    what was appended.  A switch therefore reaches the scan as one new entry
    carrying the mode - the shape a VS Code session writes - so the incremental
    path, not only the full one, has to keep the mode current.
    """

    def setUp(self) -> None:
        transcript._scan_cache.clear()

    def tearDown(self) -> None:
        transcript._scan_cache.clear()

    @staticmethod
    def _prompt(timestamp: str, permission_mode: str) -> str:
        return (
            '{"type":"user","timestamp":"' + timestamp + '","permissionMode":"' + permission_mode + '",'
            '"message":{"content":[{"type":"text","text":"go"}]}}\n'
        )

    def test_appended_prompt_updates_the_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(self._prompt('2026-07-11T09:00:00Z', 'default'), encoding='utf-8')
            self.assertEqual(transcript._scan_appended(path).permission_mode, 'default')

            with path.open('a', encoding='utf-8') as handle:
                handle.write(self._prompt('2026-07-11T09:30:00Z', 'auto'))

            self.assertEqual(transcript._scan_appended(path).permission_mode, 'auto')

    def test_partial_trailing_prompt_does_not_change_the_mode_until_complete(self) -> None:
        # A prompt entry is written while the poll may be running, so the scan can
        # see half of it. Until the line is complete it parses to nothing and the
        # previous mode stands - and the half line must not be consumed, or the
        # switch would be lost once the rest arrives.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            path.write_text(self._prompt('2026-07-11T09:00:00Z', 'default'), encoding='utf-8')
            transcript._scan_appended(path)

            complete = self._prompt('2026-07-11T09:30:00Z', 'plan')
            split = len(complete) // 2
            with path.open('a', encoding='utf-8') as handle:
                handle.write(complete[:split])

            self.assertEqual(transcript._scan_appended(path).permission_mode, 'default')

            with path.open('a', encoding='utf-8') as handle:
                handle.write(complete[split:])

            self.assertEqual(transcript._scan_appended(path).permission_mode, 'plan')


class PruneScanCacheTest(unittest.TestCase):
    """The scan cache must be evictable and must not duplicate case-variant cwds."""

    def setUp(self) -> None:
        self._previous = os.environ.get('CLAUDE_CONFIG_DIR')
        self._temp = tempfile.TemporaryDirectory()
        os.environ['CLAUDE_CONFIG_DIR'] = self._temp.name
        transcript._scan_cache.clear()

    def tearDown(self) -> None:
        transcript._scan_cache.clear()
        if self._previous is None:
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
        else:
            os.environ['CLAUDE_CONFIG_DIR'] = self._previous
        self._temp.cleanup()

    def _key(self, session_id: str, cwd: str) -> str:
        return os.path.normcase(str(transcript_path(windows_root(), session_id, cwd)))

    def test_prune_evicts_entries_not_in_the_active_registry_set(self) -> None:
        transcript._scan_cache[self._key('aaa', 'd:\\proj')] = transcript._ScanState()
        transcript._scan_cache[self._key('bbb', 'd:\\other')] = transcript._ScanState()

        transcript.prune_scan_cache([(windows_root(), 'aaa', 'd:\\proj')])

        self.assertIn(self._key('aaa', 'd:\\proj'), transcript._scan_cache)
        self.assertNotIn(self._key('bbb', 'd:\\other'), transcript._scan_cache)

    def test_wsl_and_windows_roots_do_not_collide_on_the_same_session_id_and_cwd(self) -> None:
        # transcript_path resolves under each root's own config_dir, so the same
        # (session_id, cwd) string pair under two different roots names two
        # different files - prune_scan_cache must key on the resolved path
        # (root included), not the bare (session_id, cwd) pair, or evicting one
        # root's stale entry could also evict the other root's live one.
        with tempfile.TemporaryDirectory() as wsl_base:
            wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(wsl_base), proc_dir=None, temp_dir=Path(wsl_base))
            win_key = self._key('same-id', 'd:\\proj')
            wsl_key = os.path.normcase(str(transcript_path(wsl_root, 'same-id', 'd:\\proj')))
            self.assertNotEqual(win_key, wsl_key)

            transcript._scan_cache[win_key] = transcript._ScanState()
            transcript._scan_cache[wsl_key] = transcript._ScanState()

            transcript.prune_scan_cache([(wsl_root, 'same-id', 'd:\\proj')])

            self.assertNotIn(win_key, transcript._scan_cache)
            self.assertIn(wsl_key, transcript._scan_cache)

    def test_case_variant_cwds_share_one_cache_entry(self) -> None:
        # The two cwds differ only in case and resolve to the same file on a
        # case-insensitive filesystem, so scanning via both must not duplicate.
        slug_dir = Path(self._temp.name) / 'projects' / cwd_to_slug('d:\\proj')
        slug_dir.mkdir(parents=True)
        (slug_dir / 'aaaaaaaa.jsonl').write_text(_TURN, encoding='utf-8')

        transcript._scan_appended(transcript_path(windows_root(), 'aaaaaaaa', 'd:\\proj'))
        transcript._scan_appended(transcript_path(windows_root(), 'aaaaaaaa', 'D:\\Proj'))

        self.assertEqual(len(transcript._scan_cache), 1)


if __name__ == '__main__':
    unittest.main()
