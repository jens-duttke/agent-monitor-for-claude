"""Tests for the git overview: the status parser, a real repository with a remote, and the never-waiting cache."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from agent_monitor_for_claude import git_overview
from agent_monitor_for_claude.git_status import _git_executable


def _make_writable_and_retry(function, path, _info) -> None:
    """git writes its objects read-only, which a plain delete refuses on Windows."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


class ParseStatusV2Test(unittest.TestCase):
    def test_reads_the_branch_its_distance_and_the_file_counts(self) -> None:
        output = '\0'.join([
            '# branch.oid abc', '# branch.head feature/x', '# branch.upstream origin/feature/x', '# branch.ab +2 -1',
            '1 .M N... 100644 100644 100644 a b src/a.py', '2 R. N... 100644 100644 100644 a b R100 new.py', 'old.py',
            'u UU N... 1 2 3 4 a b c conflict.py', '? notes.txt', '',
        ])

        parsed = git_overview.parse_status_v2(output)

        self.assertEqual(parsed, {
            'branch': 'feature/x', 'detached': False, 'upstream': 'origin/feature/x', 'upstream_gone': False,
            'ahead': 2, 'behind': 1, 'changed': 3, 'untracked': 1,
        })

    def test_an_upstream_without_a_distance_is_gone_from_the_remote(self) -> None:
        parsed = git_overview.parse_status_v2('# branch.head bug/y\0# branch.upstream origin/bug/y\0')

        self.assertTrue(parsed['upstream_gone'])
        self.assertEqual(parsed['ahead'], 0)

    def test_a_detached_head_has_no_branch(self) -> None:
        parsed = git_overview.parse_status_v2('# branch.head (detached)\0')

        self.assertEqual((parsed['branch'], parsed['detached'], parsed['upstream_gone']), ('', True, False))


class RemoteWebAddressTest(unittest.TestCase):
    """A remote URL becomes https://host/owner/repo - with no user, token, port or .git ever passed on - or nothing."""

    def test_the_usual_remote_forms_read_as_one_web_address(self) -> None:
        cases = {
            'git@github.com:owner/repo.git': 'https://github.com/owner/repo',
            'ssh://git@gitlab.example.com:2222/team/sub/app.git': 'https://gitlab.example.com/team/sub/app',
            'https://user:s3cr3t-token@gitlab.example.com/a/b.git': 'https://gitlab.example.com/a/b',
            'https://GitHub.com/Owner/Repo': 'https://github.com/Owner/Repo',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(git_overview.remote_web_address(url), expected)
                self.assertNotIn('token', git_overview.remote_web_address(url))

    def test_anything_else_gives_no_address(self) -> None:
        for url in ('C:/repos/x', '/srv/git/x.git', 'file:///x/y', 'https://evil.com/a/<b>', 'https://host/onlyone', 'javascript:alert(1)', '', None):
            with self.subTest(url=url):
                self.assertEqual(git_overview.remote_web_address(url), '')


@unittest.skipIf(_git_executable() is None, 'git is not installed')
class ReadDirectoryTest(unittest.TestCase):
    """A clone of a local bare repository: every remote answer comes from refs the test itself made."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, onerror=_make_writable_and_retry)
        self.remote = os.path.join(self.base, 'remote.git')
        self.work = os.path.join(self.base, 'work')
        self._git(self.base, 'init', '--bare', '-b', 'main', self.remote)
        self._git(self.base, 'clone', self.remote, self.work)
        self._commit('a.txt', 'one\n', 'first')
        self._git(self.work, 'push', '-u', 'origin', 'main')
        self._git(self.work, 'remote', 'set-head', 'origin', 'main')

    def _git(self, directory: str, *args: str) -> str:
        environment = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        result = subprocess.run(['git', '-C', directory, *args], capture_output=True, text=True, env=environment, check=True)
        return result.stdout

    def _commit(self, name: str, text: str, message: str) -> None:
        with open(os.path.join(self.work, name), 'w', encoding='utf-8') as handle:
            handle.write(text)
        self._git(self.work, 'add', name)
        self._git(self.work, 'commit', '-m', message)

    def test_a_pushed_branch_with_local_work_reports_both(self) -> None:
        self._git(self.work, 'switch', '-c', 'feature')
        self._commit('b.txt', 'two\n', 'second')
        self._git(self.work, 'push', '-u', 'origin', 'feature')
        self._commit('c.txt', 'three\n', 'third')
        with open(os.path.join(self.work, 'a.txt'), 'a', encoding='utf-8') as handle:
            handle.write('changed\n')

        record = git_overview.read_directory(self.work)

        self.assertEqual(record['branch'], 'feature')
        self.assertEqual((record['on_remote'], record['ahead'], record['changed'], record['untracked']), (True, 1, 1, 0))
        self.assertEqual((record['default_branch'], record['unmerged']), ('main', 2))

    def test_a_branch_never_pushed_is_only_local(self) -> None:
        self._git(self.work, 'switch', '-c', 'local-only')
        self._commit('b.txt', 'two\n', 'second')

        record = git_overview.read_directory(self.work)

        self.assertEqual((record['upstream'], record['on_remote'], record['ahead'], record['unmerged']), ('', False, None, 1))

    def test_a_squash_merged_and_deleted_branch_reads_as_merged(self) -> None:
        self._git(self.work, 'switch', '-c', 'done')
        self._commit('b.txt', 'two\n', 'second')
        self._git(self.work, 'push', '-u', 'origin', 'done')
        # The remote squashes the branch into main and deletes it; this clone then fetches with prune.
        self._git(self.work, 'switch', 'main')
        self._git(self.work, 'merge', '--squash', 'done')
        self._git(self.work, 'commit', '-m', 'squashed')
        self._git(self.work, 'push', 'origin', 'main', ':done')
        self._git(self.work, 'switch', 'done')
        self._git(self.work, 'fetch', '--prune')

        record = git_overview.read_directory(self.work)

        self.assertTrue(record['upstream_gone'])
        self.assertFalse(record['on_remote'])
        self.assertEqual((record['unmerged'], record['changes_in_default']), (1, True))
        self.assertIsNotNone(record['fetched_at'])

    def test_no_repository_and_no_directory_read_as_none(self) -> None:
        self.assertIsNone(git_overview.read_directory(self.base))
        self.assertIsNone(git_overview.read_directory(os.path.join(self.base, 'missing')))


class CacheTest(unittest.TestCase):
    """The overview answers at once: pending first, the record once read, the old record while it refreshes."""

    def setUp(self) -> None:
        git_overview._entries.clear()
        self.addCleanup(git_overview._entries.clear)

    def test_the_first_answer_is_pending_and_a_later_one_the_record(self) -> None:
        release = threading.Event()
        done = threading.Event()

        def slow_read(directory: str) -> dict[str, str]:
            release.wait(5)
            return {'branch': 'main'}

        def finish(key: str, directory: str) -> None:
            original_refresh(key, directory)
            done.set()

        original_refresh = git_overview._refresh
        with mock.patch.object(git_overview, 'read_directory', side_effect=slow_read), \
             mock.patch.object(git_overview, '_refresh', side_effect=finish), \
             mock.patch.object(git_overview.os.path, 'isdir', return_value=True):
            self.assertEqual(git_overview.git_overview(['C:\\repo']), [git_overview.PENDING])
            # Asked again while the read runs: still pending, and no second read starts.
            self.assertEqual(git_overview.git_overview(['C:\\repo']), [git_overview.PENDING])
            release.set()
            self.assertTrue(done.wait(5))

        self.assertEqual(git_overview.git_overview(['C:\\repo']), [{'branch': 'main'}])

    def test_an_outdated_record_is_shown_while_it_refreshes(self) -> None:
        key = os.path.normcase(os.path.normpath('C:\\repo'))
        git_overview._entries[key] = git_overview._Entry(read_at=-1e9, record={'branch': 'old'})
        with mock.patch.object(git_overview._pool, 'submit') as submit:
            self.assertEqual(git_overview.git_overview(['C:\\repo']), [{'branch': 'old'}])

        submit.assert_called_once()
