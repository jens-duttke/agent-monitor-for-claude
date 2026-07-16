"""Concurrency guard for the incremental usage scan (no double-counting)."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import transcript

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


if __name__ == '__main__':
    unittest.main()
