"""Tests for the single-instance replace path (stale-holder-PID guard)."""
from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from agent_monitor_for_claude import single_instance

_IDYES = 6
_IDNO = 7


class ReplacePathTest(unittest.TestCase):
    """The 'replace running instance' flow must never terminate a stale PID.

    The holder PID is read for the dialog, but the modal dialog can sit open
    indefinitely. If the holder exits meanwhile, its shared-memory mapping is
    released with it, so the PID can be recycled by the OS onto an unrelated
    process. The replace path must re-read the holder at click time and skip the
    kill when no live holder still claims the mapping.
    """

    def _drive_replace(self, read_results, answer=_IDYES, sole_owner=True):
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
             mock.patch.dict(single_instance.T, {'app_title': 'X', 'already_running': 'running {running_version}'}, clear=False), \
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

    def test_replace_fails_when_the_old_instance_survives(self) -> None:
        # The terminate did not take (elevated old instance, or the wait timed
        # out): the mutex still pre-exists after re-creating it, so the replace
        # failed. This instance must exit (False) and must NOT overwrite the
        # holder record - otherwise two instances run and the record is hijacked.
        result, terminated, stored = self._drive_replace([(1234, '0.3.0'), (1234, '0.3.0')], sole_owner=False)
        self.assertFalse(result)
        self.assertFalse(stored, 'a failed replace must not claim the holder record')


if __name__ == '__main__':
    unittest.main()
