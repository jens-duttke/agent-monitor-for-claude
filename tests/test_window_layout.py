"""Tests for the compact-view window sizing and its bridge methods."""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import window_layout
from agent_monitor_for_claude.app import _MonitorApi
from agent_monitor_for_claude.window_layout import COMPACT_WIDTH, NORMAL_MIN_SIZE, enter_compact, leave_compact


class _FakeWindow:
    """Stands in for a pywebview window: a size, a log of calls and an on_top flag."""

    def __init__(self, width: int = 920, height: int = 680) -> None:
        self.width = width
        self.height = height
        self.on_top = False
        self.native = object()
        self.events: list[str] = []
        self.resizes: list[tuple[int, int]] = []

    def resize(self, width: int, height: int) -> None:
        self.events.append('resize')
        self.resizes.append((width, height))
        self.width = width
        self.height = height


class _ClosedWindow:
    """A window whose size can no longer be read or changed."""

    native = None

    @property
    def width(self) -> int:
        raise RuntimeError('closed')

    @property
    def height(self) -> int:
        raise RuntimeError('closed')

    def resize(self, width: int, height: int) -> None:
        raise RuntimeError('closed')


class EnterLeaveCompactTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(window_layout, 'set_min_window_size', return_value=True)
        self.set_min = patcher.start()
        self.addCleanup(patcher.stop)

    def _record_min_calls(self, window: _FakeWindow) -> None:
        self.set_min.side_effect = lambda *args: window.events.append('min')

    def test_enter_narrows_to_the_compact_width_and_keeps_the_height(self) -> None:
        window = _FakeWindow(1100, 700)

        self.assertEqual(enter_compact(window), (1100, 700))
        self.assertEqual(window.resizes, [(COMPACT_WIDTH, 700)])

    def test_enter_lowers_the_minimum_before_resizing(self) -> None:
        # A toolkit clamps a resize to the minimum in force, so the order matters.
        window = _FakeWindow()
        self._record_min_calls(window)

        enter_compact(window)

        self.assertEqual(window.events, ['min', 'resize'])

    def test_leave_restores_the_previous_size_then_the_normal_minimum(self) -> None:
        # Raising the minimum first would make the toolkit push the window out
        # on its own before the restore lands.
        window = _FakeWindow(COMPACT_WIDTH, 500)
        self._record_min_calls(window)

        self.assertTrue(leave_compact(window, (1100, 700)))
        self.assertEqual((window.width, window.height), (1100, 700))
        self.assertEqual(window.events, ['resize', 'min'])
        self.set_min.assert_called_with(window.native, *NORMAL_MIN_SIZE)

    def test_leave_without_a_recorded_size_widens_to_the_normal_minimum(self) -> None:
        window = _FakeWindow(COMPACT_WIDTH, 500)

        leave_compact(window, None)

        self.assertEqual((window.width, window.height), (NORMAL_MIN_SIZE[0], 500))

    def test_leave_never_restores_below_the_normal_minimum(self) -> None:
        window = _FakeWindow(COMPACT_WIDTH, 200)

        leave_compact(window, (100, 100))

        self.assertEqual((window.width, window.height), NORMAL_MIN_SIZE)

    def test_a_closed_window_degrades_without_raising(self) -> None:
        window = _ClosedWindow()

        self.assertIsNone(enter_compact(window))
        self.assertFalse(leave_compact(window, (920, 680)))


class SetCompactBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(window_layout, 'set_min_window_size', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.window = _FakeWindow(1000, 700)
        self.api = _MonitorApi()
        self.api.attach_window(self.window)

    def test_round_trip_restores_the_size_before_compact(self) -> None:
        self.assertTrue(self.api.set_compact(True))
        self.assertEqual(self.window.width, COMPACT_WIDTH)

        self.assertTrue(self.api.set_compact(False))
        self.assertEqual((self.window.width, self.window.height), (1000, 700))

    def test_a_repeated_report_changes_nothing(self) -> None:
        # A reloaded page reports the stored preference again; recording the
        # compact size as the one to restore would lose the real one for good.
        self.api.set_compact(True)
        self.api.set_compact(True)
        self.api.set_compact(False)

        self.assertEqual((self.window.width, self.window.height), (1000, 700))
        self.assertEqual(len(self.window.resizes), 2)

    def test_reporting_off_while_off_does_not_resize(self) -> None:
        self.assertTrue(self.api.set_compact(False))
        self.assertEqual(self.window.resizes, [])

    def test_non_bool_is_refused(self) -> None:
        self.assertFalse(self.api.set_compact('true'))
        self.assertFalse(self.api.set_compact(1))
        self.assertEqual(self.window.resizes, [])

    def test_without_a_window_it_is_a_no_op(self) -> None:
        self.assertFalse(_MonitorApi().set_compact(True))


class _NoOnTopWindow(_FakeWindow):
    """A window whose pywebview ``on_top`` must never be assigned."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == 'on_top' and 'on_top' in self.__dict__:
            raise AssertionError('pywebview on_top assigned from the calling thread')
        super().__setattr__(name, value)


class SetOnTopBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(window_layout, 'set_keep_on_top', return_value=True)
        self.set_keep_on_top = patcher.start()
        self.addCleanup(patcher.stop)

        self.window = _NoOnTopWindow()
        self.api = _MonitorApi()
        self.api.attach_window(self.window)

    def test_goes_through_the_platform_layer(self) -> None:
        # pywebview's own on_top assigns WinForms' TopMost from the bridge
        # thread while holding the GIL; with another bridge call running on the
        # UI thread at that moment, the whole application hung at start-up.
        self.assertTrue(self.api.set_on_top(True))
        self.assertTrue(self.api.set_on_top(False))

        self.assertEqual(self.set_keep_on_top.call_args_list, [mock.call(self.window.native, True), mock.call(self.window.native, False)])
        self.assertFalse(self.window.on_top)

    def test_non_bool_is_refused(self) -> None:
        self.assertFalse(self.api.set_on_top('yes'))
        self.set_keep_on_top.assert_not_called()

    def test_without_a_window_it_is_a_no_op(self) -> None:
        self.assertFalse(_MonitorApi().set_on_top(True))
        self.set_keep_on_top.assert_not_called()


if __name__ == '__main__':
    unittest.main()
