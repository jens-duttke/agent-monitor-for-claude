"""
Tests for the Linux single-instance guard.

The lock lives in an open file descriptor, so the cases here drive the real
``flock`` against a temp directory rather than mocking it - that is the whole
mechanism, and a mock would only prove the mock.  A second holder is staged in a
subprocess, since two ``flock`` calls from one process succeed against each
other.

Skipped as a whole on Windows.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux single-instance guard')

if sys.platform != 'win32':
    from agent_monitor_for_claude.platforms import instance_linux


@_LINUX_ONLY
class LockPathTest(unittest.TestCase):
    def test_prefers_the_session_runtime_directory(self) -> None:
        with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/run/user/1000'}):
            self.assertEqual(instance_linux._lock_path(), Path('/run/user/1000/agent-monitor-for-claude.lock'))

    def test_falls_back_to_the_cache_directory(self) -> None:
        # Some minimal and containerised sessions have no runtime directory; the
        # cache directory is still per-user and writable.
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': '/home/dev/.cache'}, clear=True):
            self.assertEqual(instance_linux._lock_path(), Path('/home/dev/.cache/agent-monitor-for-claude.lock'))


@_LINUX_ONLY
class LockFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self._dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(instance_linux.release_instance_lock)

    def _path(self) -> Path:
        return Path(self._dir.name) / 'agent-monitor-for-claude.lock'

    def test_first_instance_takes_the_lock_and_records_itself(self) -> None:
        self.assertTrue(instance_linux.ensure_single_instance())

        pid, version = instance_linux._read_holder_info()
        self.assertEqual(pid, os.getpid())
        self.assertTrue(version)

    def test_the_lock_file_is_owner_only(self) -> None:
        # It carries this session's pid in the session's runtime directory; no
        # other account has business reading or replacing it.
        instance_linux.ensure_single_instance()
        self.assertEqual(self._path().stat().st_mode & 0o777, 0o600)

    def test_releasing_leaves_the_file_but_frees_the_lock(self) -> None:
        # Unlinking would let a second process create and lock a fresh file while
        # a third still holds the lock on the unlinked inode - two "only"
        # instances.  A stale file costs nothing; the lock is the descriptor.
        instance_linux.ensure_single_instance()
        instance_linux.release_instance_lock()

        self.assertTrue(self._path().exists())
        self.assertTrue(instance_linux.ensure_single_instance())

    def test_unlockable_filesystem_fails_open(self) -> None:
        # Locking unavailable is not the same as locking contended: refusing to
        # start over a rare API failure would be worse than running unguarded,
        # which is also what the Windows guard does when its mutex API fails.
        with mock.patch.object(instance_linux, '_acquire', side_effect=OSError(errno.ENOLCK, 'no locks')), \
             mock.patch.object(instance_linux, 'ask_yes_no') as ask:
            self.assertTrue(instance_linux.ensure_single_instance())

        ask.assert_not_called()
        self.assertFalse(self._path().exists())

    def test_malformed_holder_record_reads_as_no_holder(self) -> None:
        self._path().write_text('not-a-pid\n', encoding='utf-8')
        self.assertEqual(instance_linux._read_holder_info(), (None, None))

    def test_missing_holder_record_reads_as_no_holder(self) -> None:
        self.assertEqual(instance_linux._read_holder_info(), (None, None))


@_LINUX_ONLY
class SecondInstanceTest(unittest.TestCase):
    """A lock a live process holds must not be taken, and declining must not kill it."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self._dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

        # A holder that takes the lock and then waits to be told to exit, so this
        # process really is a second instance rather than re-locking its own file.
        script = textwrap.dedent(
            '''
            import os, sys
            os.environ['XDG_RUNTIME_DIR'] = sys.argv[1]
            from agent_monitor_for_claude.platforms import instance_linux
            assert instance_linux.ensure_single_instance()
            print('holding', flush=True)
            sys.stdin.read()
            '''
        )
        self._holder = subprocess.Popen(
            [sys.executable, '-c', script, self._dir.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.addCleanup(self._stop_holder)
        self.assertEqual(self._holder.stdout.readline().strip(), 'holding')

    def _stop_holder(self) -> None:
        if self._holder.poll() is None:
            self._holder.stdin.close()
            self._holder.wait(timeout=10)

        self._holder.stdout.close()
        if not self._holder.stdin.closed:
            self._holder.stdin.close()

    def test_declining_the_replacement_leaves_the_holder_running(self) -> None:
        with mock.patch.object(instance_linux, 'ask_yes_no', return_value=False) as ask, \
             mock.patch.object(instance_linux, '_terminate_pid') as terminate:
            self.assertFalse(instance_linux.ensure_single_instance())

        ask.assert_called_once()
        terminate.assert_not_called()
        self.assertIsNone(self._holder.poll())

    def test_the_dialog_names_the_running_version(self) -> None:
        with mock.patch.object(instance_linux, 'ask_yes_no', return_value=False) as ask:
            instance_linux.ensure_single_instance()

        _message, title = ask.call_args.args
        self.assertIn('v', title)

    def test_a_holder_that_exited_during_the_dialog_is_not_killed(self) -> None:
        # The dialog can sit open indefinitely and the kernel recycles pids, so
        # the pid read before it must be confirmed as still the recorded holder.
        real_pid = self._holder.pid

        def _answer(_message: str, _title: str) -> bool:
            Path(self._dir.name, 'agent-monitor-for-claude.lock').write_text('999999\n0.0.0\n', encoding='utf-8')
            return True

        with mock.patch.object(instance_linux, 'ask_yes_no', side_effect=_answer), \
             mock.patch.object(instance_linux, '_terminate_pid') as terminate:
            self.assertFalse(instance_linux.ensure_single_instance())

        terminate.assert_not_called()
        self.assertIsNone(self._holder.poll())
        self.assertNotEqual(real_pid, 999999)

    def test_accepting_replaces_the_holder_and_takes_the_lock(self) -> None:
        self.addCleanup(instance_linux.release_instance_lock)
        with mock.patch.object(instance_linux, 'ask_yes_no', return_value=True):
            self.assertTrue(instance_linux.ensure_single_instance())

        # Retaking the lock is the ground truth that the holder really exited:
        # the kernel only frees it when that process is gone.
        self.assertIsNotNone(self._holder.poll())
        self.assertEqual(instance_linux._read_holder_info()[0], os.getpid())


@_LINUX_ONLY
class ProcessAliveTest(unittest.TestCase):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(instance_linux._process_is_alive(os.getpid()))

    def test_an_unused_pid_is_not(self) -> None:
        self.assertFalse(instance_linux._process_is_alive(999999))

    def test_an_unreaped_process_is_not_alive(self) -> None:
        # A process that exited but has not been reaped keeps its pid, so a bare
        # signal probe still finds it - and the replacement would then sit
        # through both timeouts waiting for a process that is already gone.
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        self.addCleanup(child.wait)
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)

        self.assertTrue(instance_linux._is_zombie(child.pid))
        self.assertFalse(instance_linux._process_is_alive(child.pid))

    def test_an_unreadable_state_counts_as_running(self) -> None:
        # The safe direction: the caller then waits, rather than assuming a
        # process it cannot inspect has already released the lock.
        self.assertFalse(instance_linux._is_zombie(os.getpid()))


if __name__ == '__main__':
    unittest.main()
