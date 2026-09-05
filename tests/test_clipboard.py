"""Tests for the clipboard bridge validation (the copy_text guards)."""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import clipboard
from agent_monitor_for_claude.app import _MonitorApi


class CopyTextBridgeTest(unittest.TestCase):
    """The JS bridge must reject junk and only forward real strings to the platform."""

    def test_rejects_non_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app._copy_text') as copy:
            self.assertFalse(api.copy_text(123))
            self.assertFalse(api.copy_text(None))
            self.assertFalse(api.copy_text(True))
            copy.assert_not_called()

    def test_rejects_empty_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app._copy_text') as copy:
            self.assertFalse(api.copy_text(''))
            copy.assert_not_called()

    def test_forwards_valid_string(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app._copy_text', return_value=True) as copy:
            self.assertTrue(api.copy_text('a7a12d93-e700-4d96-b024-689a35c12bc2'))
            copy.assert_called_once_with('a7a12d93-e700-4d96-b024-689a35c12bc2')

    def test_propagates_copy_failure(self) -> None:
        api = _MonitorApi()
        with mock.patch('agent_monitor_for_claude.app._copy_text', return_value=False):
            self.assertFalse(api.copy_text('session-id'))


class CopyTextGuardTest(unittest.TestCase):
    """The shared guard refuses before the platform is ever asked to write."""

    def test_empty_and_non_string_never_reach_the_platform(self) -> None:
        # Emptying the user's clipboard is not what a failed copy should do, so
        # a value that cannot be copied is refused before the write starts.
        with mock.patch.object(clipboard, '_platform_copy_text') as write:
            for bad in ('', None, 123, True):
                self.assertFalse(clipboard.copy_text(bad))
            write.assert_not_called()

    def test_a_real_string_is_forwarded(self) -> None:
        with mock.patch.object(clipboard, '_platform_copy_text', return_value=True) as write:
            self.assertTrue(clipboard.copy_text('session-id'))
            write.assert_called_once_with('session-id')


if __name__ == '__main__':
    unittest.main()
