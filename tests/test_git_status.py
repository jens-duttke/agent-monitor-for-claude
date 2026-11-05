"""
Git status tests
================

The parsing of git's porcelain and numstat output, and one real repository
when git is installed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import git_status as gmod


class ParseStatusTest(unittest.TestCase):
    def test_the_branch_line_and_the_entries_are_split(self) -> None:
        output = '## main...origin/main [ahead 2, behind 1]\0 M src/a.py\0?? notes.txt\0R  new.py\0old.py\0'
        branch, upstream, ahead, behind, entries = gmod._parse_status(output)
        self.assertEqual((branch, upstream, ahead, behind), ('main', 'origin/main', 2, 1))
        self.assertEqual(entries, [(' M', 'src/a.py'), ('??', 'notes.txt'), ('R ', 'new.py')])

    def test_a_branch_without_upstream_or_commit(self) -> None:
        self.assertEqual(gmod._parse_branch('feature'), ('feature', '', 0, 0))
        self.assertEqual(gmod._parse_branch('No commits yet on main'), ('main', '', 0, 0))

    def test_numstat_counts_lines_and_leaves_a_binary_file_without(self) -> None:
        lines = gmod._parse_numstat('3\t1\tsrc/a.py\0-\t-\timage.png\0garbage\0')
        self.assertEqual(lines, {'src/a.py': (3, 1), 'image.png': (None, None)})

    def test_numstat_counts_a_rename_under_its_new_path_and_keeps_paths_unescaped(self) -> None:
        lines = gmod._parse_numstat('2\t0\t\0old/a.py\0new/a.py\0' '1\t1\tdocs/Übersicht.md\0' '4\t0\t\0truncated')
        self.assertEqual(lines, {'new/a.py': (2, 0), 'docs/Übersicht.md': (1, 1)})


class GitExecutableTest(unittest.TestCase):
    def test_a_relative_path_entry_is_never_searched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            name = 'git.exe' if gmod.platforms.IS_WINDOWS else 'git'
            (Path(directory) / name).write_bytes(b'')
            with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['.', '', 'relative'])}):
                self.assertIsNone(gmod._git_executable())
            with mock.patch.dict(os.environ, {'PATH': directory}):
                self.assertEqual(gmod._git_executable(), str(Path(directory) / name))


class GitStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        gmod._cache.clear()

    def test_no_directory_no_status(self) -> None:
        self.assertIsNone(gmod.git_status(''))
        self.assertIsNone(gmod.git_status(os.path.join(tempfile.gettempdir(), 'surely-not-here-4711')))

    @unittest.skipIf(gmod._git_executable() is None, 'git is not installed')
    def test_a_real_repository_reports_its_uncommitted_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def git(*args: str) -> None:
                subprocess.run(['git', '-C', directory, *args], check=True, capture_output=True,
                               env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t'))

            git('init', '-q')
            tracked = Path(directory) / 'tracked.txt'
            tracked.write_text('one\ntwo\n', encoding='utf-8')
            git('add', 'tracked.txt')
            git('commit', '-q', '-m', 'first')
            moving = Path(directory) / 'notes.txt'
            moving.write_text('a\nb\nc\nd\ne\n', encoding='utf-8')
            git('add', 'notes.txt')
            git('commit', '-q', '-m', 'second')
            tracked.write_text('one\nthree\nfour\n', encoding='utf-8')
            (Path(directory) / 'new.txt').write_text('x\n', encoding='utf-8')
            git('mv', 'notes.txt', 'Übersicht.txt')
            (Path(directory) / 'Übersicht.txt').write_text('a\nb\nc\nd\ne\nf\n', encoding='utf-8')
            git('add', 'Übersicht.txt')
            committed = Path(directory) / 'committed.txt'

            result = gmod.git_status(directory, [str(tracked), str(committed), os.path.join(tempfile.gettempdir(), 'elsewhere.txt')])

        self.assertIsNotNone(result)
        self.assertEqual((result['changed'], result['untracked']), (2, 1))
        self.assertEqual((result['added'], result['removed']), (3, 1))
        self.assertEqual((result['edited_total'], result['edited_uncommitted']), (2, 1))
        by_path = {record['path']: record for record in result['files']}
        self.assertTrue(by_path['tracked.txt']['edited'])
        self.assertEqual(by_path['new.txt']['status'], '??')
        # A renamed file keeps its line counts, and a non-ASCII name its own spelling.
        self.assertEqual((by_path['Übersicht.txt']['status'], by_path['Übersicht.txt']['added']), ('R', 1))

    def test_a_directory_outside_any_repository_reports_none(self) -> None:
        with mock.patch.object(gmod, '_git', return_value=None):
            with tempfile.TemporaryDirectory() as directory:
                self.assertIsNone(gmod.git_status(directory))


if __name__ == '__main__':
    unittest.main()
