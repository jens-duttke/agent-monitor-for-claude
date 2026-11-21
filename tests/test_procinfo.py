"""Tests for the platform-neutral process types: a session's background processes as a tree."""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import procinfo
from agent_monitor_for_claude.procinfo import ChildProcess, child_processes, clip_arguments, program_names

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

    def test_each_process_carries_its_start_time_where_known(self) -> None:
        # What lets the status tell a process the pending call started from one
        # an earlier call left running.
        tree = child_processes(_SESSION, [(300, 'a'), (400, 'b')], {300: _SESSION, 400: _SESSION}, {300: 1700000000.5})

        self.assertEqual([(child.pid, child.started_at) for child in tree], [(300, 1700000000.5), (400, None)])


class ChildStartOnTheRunningSystemTest(unittest.TestCase):
    def test_a_child_just_started_reads_as_just_started(self) -> None:
        import os
        import subprocess
        import sys
        import time

        from agent_monitor_for_claude.process_probe import probe_all

        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        time.sleep(0.5)

        # This test process is itself seconds old, so its child would read as a
        # helper started with the session and not be listed at all.
        from agent_monitor_for_claude import procfs
        from agent_monitor_for_claude.platforms import IS_WINDOWS
        if IS_WINDOWS:
            from agent_monitor_for_claude.platforms import process_win32 as backend
        else:
            backend = procfs
        with mock.patch.object(backend, 'SESSION_HELPER_WINDOW_SECONDS', -1):
            info = probe_all([(os.getpid(), None)])[os.getpid()]
        started = {process.pid: process.started_at for process in info.children}

        self.assertIn(child.pid, started)
        self.assertLess(abs(time.time() - started[child.pid]), 15)


class ProgramNamesTest(unittest.TestCase):
    """The names a command line runs a program under: its executable, and for an interpreter the script it runs."""

    def test_a_native_program_is_named_by_its_executable(self) -> None:
        self.assertEqual(program_names(['/usr/bin/rust-analyzer']), ('rust-analyzer',))
        self.assertEqual(program_names(['cargo', 'check']), ('cargo',))
        self.assertEqual(program_names(['C:/Users/u/.cargo/bin/rust-analyzer.exe']), ('rust-analyzer',))

    def test_a_script_run_by_an_interpreter_is_named_by_the_script_too(self) -> None:
        # pipx starts pyright through its venv's python; a node server through node.
        self.assertEqual(program_names(['/home/u/.local/share/pipx/venvs/pyright/bin/python', '/home/u/.local/bin/pyright-langserver', '--stdio']),
                         ('python', 'pyright-langserver'))
        self.assertEqual(program_names(['node', '/usr/bin/typescript-language-server', '--stdio']), ('node', 'typescript-language-server'))
        self.assertEqual(program_names([r'C:\Program Files\nodejs\node.exe', r'C:\srv\server.js']), ('node', 'server'))
        self.assertEqual(program_names(['python3.12', '-u', 'srv.py']), ('python3', 'srv'))

    def test_an_ordinary_program_s_arguments_are_no_name(self) -> None:
        self.assertEqual(program_names(['git', 'rust-analyzer']), ('git',))

    def test_an_empty_command_line_has_no_name(self) -> None:
        self.assertEqual(program_names([]), ())
        self.assertEqual(program_names(['']), ())
        self.assertEqual(program_names(['node']), ('node',))


if __name__ == '__main__':
    unittest.main()


class ClipArgumentsTest(unittest.TestCase):
    def test_a_short_command_line_is_kept_whole(self) -> None:
        self.assertEqual(clip_arguments(['bash', '-c', 'echo hi']), (('bash', '-c', 'echo hi'), False))

    def test_a_long_one_keeps_whole_arguments_only_and_says_so(self) -> None:
        limit = procinfo._MAX_COMMAND_LINE_CHARS
        arguments = ['node', 'a' * (limit // 2), 'b' * (limit // 2)]

        kept, clipped = clip_arguments(arguments)

        self.assertEqual(kept, ('node', 'a' * (limit // 2)))
        self.assertTrue(clipped)
