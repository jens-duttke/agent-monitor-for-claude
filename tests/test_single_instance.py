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

    def _drive_replace(self, read_results, answer=_IDYES):
        terminated = []
        fake_kernel = mock.Mock()
        fake_kernel.CreateMutexW.return_value = 0x111
        fake_kernel.CloseHandle.return_value = 1

        with mock.patch.object(single_instance, '_kernel32', fake_kernel), \
             mock.patch.object(single_instance, '_read_holder_info', side_effect=list(read_results)), \
             mock.patch.object(single_instance, '_terminate_pid', side_effect=terminated.append), \
             mock.patch.object(single_instance, '_store_holder_info'), \
             mock.patch.dict(single_instance.T, {'app_title': 'X', 'already_running': 'running {running_version}'}, clear=False), \
             mock.patch.object(ctypes, 'get_last_error', return_value=single_instance._ERROR_ALREADY_EXISTS), \
             mock.patch.object(ctypes.windll.user32, 'MessageBoxW', return_value=answer):
            result = single_instance.ensure_single_instance()

        return result, terminated

    def test_replace_skips_kill_when_holder_exited_during_dialog(self) -> None:
        # Holder present when the dialog opens, gone (mapping released) at click time.
        result, terminated = self._drive_replace([(1234, '0.3.0'), (None, None)])
        self.assertTrue(result)
        self.assertEqual(terminated, [], 'a stale, since-recycled holder PID must not be terminated')

    def test_replace_kills_a_still_live_holder(self) -> None:
        # Holder alive throughout: the replace must still terminate it.
        result, terminated = self._drive_replace([(1234, '0.3.0'), (1234, '0.3.0')])
        self.assertTrue(result)
        self.assertEqual(terminated, [1234])

    def test_declining_replace_returns_false_without_kill(self) -> None:
        result, terminated = self._drive_replace([(1234, '0.3.0')], answer=_IDNO)
        self.assertFalse(result)
        self.assertEqual(terminated, [])


if __name__ == '__main__':
    unittest.main()
