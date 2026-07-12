"""Tests for the clipboard bridge validation (the copy_text guard)."""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude.app import _MonitorApi


class CopyTextBridgeTest(unittest.TestCase):
    """The JS bridge must reject junk and only forward real strings to Win32."""

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


if __name__ == '__main__':
    unittest.main()
