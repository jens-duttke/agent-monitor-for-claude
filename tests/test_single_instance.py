"""Tests for the single-instance replace path (stale-holder-PID guard)."""
from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from agent_monitor_for_claude import single_instance

_IDYES = 6
_IDNO = 7

# Stand-in for the process handle OpenProcess hands back in the terminate tests.
_FAKE_PROCESS_HANDLE = 0x222


class ReplacePathTest(unittest.TestCase):
    """The 'replace running instance' flow must never terminate a stale PID.

    The holder PID is read for the dialog, but the modal dialog can sit open
    indefinitely. If the holder exits meanwhile, its shared-memory mapping is
    released with it, so the PID can be recycled by the OS onto an unrelated
    process. The replace path must re-read the holder at click time and skip the
    kill when no live holder still claims the mapping.
    """

    def _drive_replace(self, read_results, answer=_IDYES, sole_owner=True, translations=None):
        if translations is None:
            translations = {'app_title': 'X', 'already_running': 'running {running_version}'}
        terminated = []
        fake_kernel = mock.Mock()
        fake_kernel.CreateMutexW.return_value = 0x111
        fake_kernel.CloseHandle.return_value = 1

        # First call: report the pre-existing mutex so the replace path is taken.
        # Second call (after the terminate, re-creating the mutex): report whether
        # this instance actually became the sole owner.
        calls = {'n': 0}

        def fake_last_error():
            calls['n'] += 1
            if calls['n'] == 1:
                return single_instance._ERROR_ALREADY_EXISTS
            return 0 if sole_owner else single_instance._ERROR_ALREADY_EXISTS

        store = mock.Mock()

        with mock.patch.object(single_instance, '_kernel32', fake_kernel), \
             mock.patch.object(single_instance, '_read_holder_info', side_effect=list(read_results)), \
             mock.patch.object(single_instance, '_terminate_pid', side_effect=terminated.append), \
             mock.patch.object(single_instance, '_store_holder_info', store), \
             mock.patch.dict(single_instance.T, translations, clear=True), \
             mock.patch.object(ctypes, 'get_last_error', side_effect=fake_last_error), \
             mock.patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=answer):
            result = single_instance.ensure_single_instance()

        return result, terminated, store.called

    def test_replace_skips_kill_when_holder_exited_during_dialog(self) -> None:
        # Holder present when the dialog opens, gone (mapping released) at click time.
        result, terminated, stored = self._drive_replace([(1234, '0.3.0'), (None, None)])
        self.assertTrue(result)
        self.assertEqual(terminated, [], 'a stale, since-recycled holder PID must not be terminated')
        self.assertTrue(stored)

    def test_replace_kills_a_still_live_holder(self) -> None:
        # Holder alive throughout: the replace must still terminate it.
        result, terminated, stored = self._drive_replace([(1234, '0.3.0'), (1234, '0.3.0')])
        self.assertTrue(result)
        self.assertEqual(terminated, [1234])
        self.assertTrue(stored)

    def test_declining_replace_returns_false_without_kill(self) -> None:
        result, terminated, stored = self._drive_replace([(1234, '0.3.0')], answer=_IDNO)
        self.assertFalse(result)
        self.assertEqual(terminated, [])
        self.assertFalse(stored)

    def test_replace_survives_empty_translations(self) -> None:
        # The documented last-resort degradation: all locale candidates failed
        # and T is empty. The dialog must still build (English defaults) rather
        # than crash startup with a KeyError.
        result, terminated, stored = self._drive_replace([(1234, '0.3.0')], answer=_IDNO, translations={})
        self.assertFalse(result)
        self.assertEqual(terminated, [])

    def test_replace_fails_when_the_old_instance_survives(self) -> None:
        # The terminate did not take (elevated old instance, or the wait timed
        # out): the mutex still pre-exists after re-creating it, so the replace
        # failed. This instance must exit (False) and must NOT overwrite the
        # holder record - otherwise two instances run and the record is hijacked.
        result, terminated, stored = self._drive_replace([(1234, '0.3.0'), (1234, '0.3.0')], sole_owner=False)
        self.assertFalse(result)
        self.assertFalse(stored, 'a failed replace must not claim the holder record')


class TerminatePidTest(unittest.TestCase):
    """Terminating the previous holder: exit code, wait, and handle release.

    The replaced instance must die with a success exit code, and every path out
    of the terminate must release the process handle it opened.
    """

    def _terminate(self, process_handle=_FAKE_PROCESS_HANDLE, terminate_ok=1):
        fake_kernel = mock.Mock()
        fake_kernel.OpenProcess.return_value = process_handle
        fake_kernel.TerminateProcess.return_value = terminate_ok
        fake_kernel.CloseHandle.return_value = 1

        with mock.patch.object(single_instance, '_kernel32', fake_kernel):
            single_instance._terminate_pid(1234)

        return fake_kernel

    def test_replaced_instance_exits_with_code_zero(self) -> None:
        # A user-confirmed replace is an orderly handover. A non-zero code makes
        # a launcher waiting on this process (a tray app that started it) report
        # the replaced instance as a failed command.
        fake_kernel = self._terminate()

        fake_kernel.TerminateProcess.assert_called_once_with(_FAKE_PROCESS_HANDLE, 0)
        fake_kernel.WaitForSingleObject.assert_called_once_with(_FAKE_PROCESS_HANDLE, mock.ANY)
        fake_kernel.CloseHandle.assert_called_once_with(_FAKE_PROCESS_HANDLE)

    def test_no_terminate_when_the_process_cannot_be_opened(self) -> None:
        fake_kernel = self._terminate(process_handle=0)

        fake_kernel.TerminateProcess.assert_not_called()
        fake_kernel.CloseHandle.assert_not_called()

    def test_failed_terminate_closes_the_handle_without_waiting(self) -> None:
        fake_kernel = self._terminate(terminate_ok=0)

        fake_kernel.WaitForSingleObject.assert_not_called()
        fake_kernel.CloseHandle.assert_called_once_with(_FAKE_PROCESS_HANDLE)


class MutexCreationFailureTest(unittest.TestCase):
    """A failed CreateMutexW must be distinguished from a fresh creation."""

    def test_failed_mutex_creation_does_not_write_a_holder_record(self) -> None:
        # CreateMutexW returned NULL with a non-ALREADY_EXISTS error: the mutex
        # could not be created, so single-instancing degrades to off. The app
        # still runs (fail open), but it must not write a holder record it does
        # not back with a held mutex, nor treat the failure as a fresh creation.
        _ERROR_ACCESS_DENIED = 5
        fake_kernel = mock.Mock()
        fake_kernel.CreateMutexW.return_value = 0  # NULL handle: creation failed
        fake_kernel.CloseHandle.return_value = 1
        store = mock.Mock()

        with mock.patch.object(single_instance, '_kernel32', fake_kernel), \
             mock.patch.object(single_instance, '_store_holder_info', store), \
             mock.patch.object(ctypes, 'get_last_error', return_value=_ERROR_ACCESS_DENIED):
            result = single_instance.ensure_single_instance()

        self.assertTrue(result, 'a rare mutex API failure should not block startup')
        self.assertFalse(store.called, 'no holder record without a held mutex')
        self.assertIsNone(single_instance._mutex_handle)


if __name__ == '__main__':
    unittest.main()
