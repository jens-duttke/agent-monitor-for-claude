"""
Tests for the window-background helpers.

``window_background_color`` maps the Windows app theme to a content colour, and
``_colorref`` converts a ``#rrggbb`` string to a Win32 COLORREF.  These cover
the theme mapping (light, dark, missing preference) and the colour conversion,
including its byte order and rejection of malformed input, plus the input
guards of ``apply_native_background`` (the .NET interop itself needs a live
WebView2 host and is not exercised here).
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import window_background
from agent_monitor_for_claude.window_background import apply_native_background, _colorref, window_background_color


class ColorRefTest(unittest.TestCase):
    def test_byte_order_is_bgr(self) -> None:
        # #rrggbb -> 0x00bbggrr: the red byte is lowest, the blue byte highest.
        self.assertEqual(_colorref('#191918'), 0x181919)
        self.assertEqual(_colorref('#faf9f5'), 0xF5F9FA)

    def test_leading_hash_is_optional(self) -> None:
        self.assertEqual(_colorref('191918'), _colorref('#191918'))

    def test_channel_extremes(self) -> None:
        self.assertEqual(_colorref('#000000'), 0x000000)
        self.assertEqual(_colorref('#ffffff'), 0xFFFFFF)
        self.assertEqual(_colorref('#ff0000'), 0x0000FF)  # pure red lands in the low byte

    def test_malformed_returns_none(self) -> None:
        for bad in ('', None, '#fff', '#1234567', 'zzzzzz', '#gggggg'):
            self.assertIsNone(_colorref(bad))


class WindowBackgroundColorTest(unittest.TestCase):
    def _patch_registry(self, value: object | None, raises: bool = False):
        query = mock.Mock()
        if raises:
            query.side_effect = OSError('no such value')
        else:
            query.return_value = (value, 4)

        open_key = mock.MagicMock()
        open_key.return_value.__enter__.return_value = object()

        return mock.patch.multiple(window_background.winreg, OpenKey=open_key, QueryValueEx=query)

    def test_light_theme(self) -> None:
        with self._patch_registry(1):
            self.assertEqual(window_background_color(), '#faf9f5')

    def test_dark_theme(self) -> None:
        with self._patch_registry(0):
            self.assertEqual(window_background_color(), '#191918')

    def test_missing_preference_falls_back_to_dark(self) -> None:
        with self._patch_registry(None, raises=True):
            self.assertEqual(window_background_color(), '#191918')


class ApplyNativeBackgroundTest(unittest.TestCase):
    """The interop guards (no window, malformed colour) return before any .NET
    call; the interop itself only runs inside a live WebView2 host."""

    def test_none_native_is_a_no_op(self) -> None:
        self.assertFalse(apply_native_background(None, '#191918'))

    def test_malformed_colour_is_a_no_op(self) -> None:
        self.assertFalse(apply_native_background(object(), 'not-a-colour'))


if __name__ == '__main__':
    unittest.main()
