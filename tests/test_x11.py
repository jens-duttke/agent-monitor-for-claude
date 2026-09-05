"""
Tests for the X11 window access used to raise a session's window on Linux.

Enumerating and activating real windows needs a live X server, which a test
suite cannot assume - so what is pinned here is the contract that holds without
one: every entry point degrades to an empty list or ``False`` rather than
raising into a bridge call, whether the display, the library, or an entry point
inside it is missing.

Skipped as a whole on Windows.
"""
from __future__ import annotations

import ctypes
import os
import sys
import unittest
from unittest import mock

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux window access')

if sys.platform != 'win32':
    from agent_monitor_for_claude.platforms import x11


@_LINUX_ONLY
class DegradationTest(unittest.TestCase):
    """Every reason the X server can be out of reach must read the same way."""

    def setUp(self) -> None:
        # _library() caches its verdict for the process lifetime, so a test that
        # stages a missing library has to put the real one back afterwards.
        self._cached = x11._library_cache
        self._loaded = x11._library_loaded
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        x11._library_cache = self._cached
        x11._library_loaded = self._loaded

    def _forget_library(self) -> None:
        x11._library_cache = None
        x11._library_loaded = False

    def test_without_a_display_nothing_is_enumerated_or_raised(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(x11.enum_windows(), [])
            self.assertFalse(x11.activate_window(12345))

    def test_an_unloadable_library_degrades(self) -> None:
        self._forget_library()
        with mock.patch.object(ctypes, 'CDLL', side_effect=OSError('no libX11')):
            self.assertEqual(x11.enum_windows(), [])
            self.assertFalse(x11.activate_window(12345))

    def test_a_library_missing_an_entry_point_degrades(self) -> None:
        # A stripped or unexpected libX11 makes prototyping raise AttributeError;
        # that must be reported as "unavailable", not escape into the bridge.
        self._forget_library()
        with mock.patch.object(x11, '_declare', side_effect=AttributeError('XGetWindowProperty')):
            self.assertIsNone(x11._library())
            self.assertEqual(x11.enum_windows(), [])

    def test_the_verdict_is_cached_rather_than_retried(self) -> None:
        self._forget_library()
        with mock.patch.object(ctypes, 'CDLL', side_effect=OSError('no libX11')) as load:
            x11._library()
            x11._library()

        self.assertEqual(load.call_count, 1)

    def test_a_display_that_refuses_to_open_degrades(self) -> None:
        library = mock.Mock()
        library.XOpenDisplay.return_value = None
        with mock.patch.object(x11, '_library', return_value=library), \
             mock.patch.dict(os.environ, {'DISPLAY': ':0'}):
            self.assertEqual(x11.enum_windows(), [])
            self.assertFalse(x11.activate_window(12345))

        library.XCloseDisplay.assert_not_called()


@_LINUX_ONLY
class ClientListTest(unittest.TestCase):
    """Order is the contract: the caller's "first window this process owns" must mean the topmost one."""

    def _library(self, atoms: dict[bytes, int]) -> mock.Mock:
        library = mock.Mock()
        library.XDefaultRootWindow.return_value = 1
        library.XInternAtom.side_effect = lambda _display, name, _exists: atoms.get(name, 0)
        return library

    def test_stacking_order_is_reversed_into_topmost_first(self) -> None:
        # EWMH defines _NET_CLIENT_LIST_STACKING bottom-to-top.
        library = self._library({b'_NET_CLIENT_LIST_STACKING': 10})
        with mock.patch.object(x11, '_read_longs', return_value=[100, 200, 300]):
            self.assertEqual(x11._client_list(library, 1), [300, 200, 100])

    def test_mapping_order_is_the_fallback(self) -> None:
        # A window manager without the stacking list still gets a usable answer,
        # just in mapping order rather than z-order.
        library = self._library({b'_NET_CLIENT_LIST': 11})
        with mock.patch.object(x11, '_read_longs', return_value=[100, 200]):
            self.assertEqual(x11._client_list(library, 1), [100, 200])

    def test_an_empty_stacking_list_falls_back_too(self) -> None:
        library = self._library({b'_NET_CLIENT_LIST_STACKING': 10, b'_NET_CLIENT_LIST': 11})
        reads = {10: [], 11: [100]}
        with mock.patch.object(x11, '_read_longs', side_effect=lambda _lib, _d, _w, prop, _t: reads[prop]):
            self.assertEqual(x11._client_list(library, 1), [100])

    def test_neither_list_published_yields_nothing(self) -> None:
        self.assertEqual(x11._client_list(self._library({}), 1), [])


if __name__ == '__main__':
    unittest.main()
