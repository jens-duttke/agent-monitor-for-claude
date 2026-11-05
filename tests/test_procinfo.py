"""Tests for the platform-neutral process types: a session's background processes as a tree."""
from __future__ import annotations

import unittest

from agent_monitor_for_claude.procinfo import ChildProcess, child_processes

_SESSION = 100


class ChildProcessTreeTest(unittest.TestCase):
    def test_each_process_hangs_off_the_nearest_listed_one_above_it(self) -> None:
        # 100 (session) -> 200 conhost (skipped) -> 300 bash -> 400 python -> 500 tool
        #                                                     -> 600 helper (skipped) -> 700 node
        descendants = [(500, 'tool'), (300, 'bash'), (700, 'node'), (400, 'python')]
        parent_of = {200: _SESSION, 300: 200, 400: 300, 500: 400, 600: 300, 700: 600}

        tree = child_processes(_SESSION, descendants, parent_of)

        self.assertEqual(tree, (
            ChildProcess(300, 0, 'bash'),
            ChildProcess(400, 300, 'python'),
            ChildProcess(500, 400, 'tool'),
            ChildProcess(700, 300, 'node'),
        ))

    def test_an_unknown_or_looping_parent_chain_ends_at_the_session(self) -> None:
        descendants = [(300, 'a'), (400, 'b')]
        parent_of = {300: 999, 400: 401, 401: 402, 402: 401}

        self.assertEqual(child_processes(_SESSION, descendants, parent_of), (ChildProcess(300, 0, 'a'), ChildProcess(400, 0, 'b')))

    def test_no_descendants_is_an_empty_tree(self) -> None:
        self.assertEqual(child_processes(_SESSION, [], {}), ())


if __name__ == '__main__':
    unittest.main()
