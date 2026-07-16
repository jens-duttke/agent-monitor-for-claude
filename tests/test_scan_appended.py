"""Concurrency guard for the incremental usage scan (no double-counting)."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript
from agent_monitor_for_claude.paths import cwd_to_slug, transcript_path

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
            totals, *_ = transcript._scan_appended(path)
            self.assertEqual(totals['input_tokens'], 100)

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
            totals, *_ = transcript._scan_appended(path)
            self.assertEqual(totals['input_tokens'], 200)


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
        return os.path.normcase(str(transcript_path(session_id, cwd)))

    def test_prune_evicts_entries_not_in_the_active_registry_set(self) -> None:
        transcript._scan_cache[self._key('aaa', 'd:\\proj')] = transcript._ScanState()
        transcript._scan_cache[self._key('bbb', 'd:\\other')] = transcript._ScanState()

        transcript.prune_scan_cache([('aaa', 'd:\\proj')])

        self.assertIn(self._key('aaa', 'd:\\proj'), transcript._scan_cache)
        self.assertNotIn(self._key('bbb', 'd:\\other'), transcript._scan_cache)

    def test_case_variant_cwds_share_one_cache_entry(self) -> None:
        # The two cwds differ only in case and resolve to the same file on a
        # case-insensitive filesystem, so scanning via both must not duplicate.
        slug_dir = Path(self._temp.name) / 'projects' / cwd_to_slug('d:\\proj')
        slug_dir.mkdir(parents=True)
        (slug_dir / 'aaaaaaaa.jsonl').write_text(_TURN, encoding='utf-8')

        transcript._scan_appended(transcript_path('aaaaaaaa', 'd:\\proj'))
        transcript._scan_appended(transcript_path('aaaaaaaa', 'D:\\Proj'))

        self.assertEqual(len(transcript._scan_cache), 1)


if __name__ == '__main__':
    unittest.main()
