"""
Test result parsing tests
=========================

One sample of each runner's summary, and the newest run winning.
"""

from __future__ import annotations

import unittest

from agent_monitor_for_claude.test_results import TestSummary, test_summary


class TestSummaryTest(unittest.TestCase):
    def test_pytest(self) -> None:
        text = 'tests/a.py ..F\n\x1b[31m==== 1 failed, 41 passed, 2 skipped in 3.21s ====\x1b[0m\n'
        self.assertEqual(test_summary(text), TestSummary('pytest', 41, 1, 2))

    def test_unittest_ok_and_failed(self) -> None:
        self.assertEqual(test_summary('....\n' + '-' * 70 + '\nRan 220 tests in 3.364s\n\nOK\n'), TestSummary('unittest', 220, 0, 0))
        text = 'Ran 10 tests in 0.5s\n\nFAILED (failures=2, errors=1, skipped=3)\n'
        self.assertEqual(test_summary(text), TestSummary('unittest', 4, 3, 3))

    def test_jest_and_vitest(self) -> None:
        self.assertEqual(test_summary('Tests:       2 failed, 1 skipped, 30 passed, 33 total\n'), TestSummary('jest', 30, 2, 1))
        self.assertEqual(test_summary(' Tests  3 failed | 97 passed (100)\n'), TestSummary('vitest', 97, 3, 0))

    def test_node_test_runner_and_tap(self) -> None:
        text = 'ℹ tests 226\nℹ suites 0\nℹ pass 225\nℹ fail 1\nℹ cancelled 0\nℹ skipped 0\n'
        self.assertEqual(test_summary(text), TestSummary('tap', 225, 1, 0))
        self.assertEqual(test_summary('# tests 5\n# pass 5\n# fail 0\n'), TestSummary('tap', 5, 0, 0))

    def test_cargo_and_go(self) -> None:
        text = 'test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured\n'
        self.assertEqual(test_summary(text), TestSummary('cargo', 12, 0, 1))
        self.assertEqual(test_summary('ok  \texample.com/a\t0.1s\nFAIL\texample.com/b\t0.2s\n'), TestSummary('go', 1, 1, 0))
        # A line that merely opens with "ok" is no package line, tabs elsewhere or not.
        self.assertIsNone(test_summary('ok done, moving on\nname\tvalue\n'))

    def test_the_newest_run_wins(self) -> None:
        text = '=== 3 failed, 1 passed in 1.0s ===\nwatching...\n=== 4 passed in 0.9s ===\n'
        summary = test_summary(text)
        self.assertEqual(summary, TestSummary('pytest', 4, 0, 0))
        self.assertTrue(summary.ok)

    def test_ordinary_output_has_no_summary(self) -> None:
        self.assertIsNone(test_summary('Compiling...\nOK, done.\nok\n'))
        self.assertIsNone(test_summary(''))


if __name__ == '__main__':
    unittest.main()
