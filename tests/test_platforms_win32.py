"""
Tests for the Windows platform backend.

Skipped as a whole off Windows: every case here reaches a Win32 or registry
entry point that does not exist elsewhere.  The counterparts live in
``test_platforms_linux.py``.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_WINDOWS_ONLY = unittest.skipUnless(sys.platform == 'win32', 'Windows backend')

if sys.platform == 'win32':
    from agent_monitor_for_claude.platforms import win32
else:  # pragma: no cover - the module cannot be imported off Windows
    win32 = None


class _FakeStream:
    """A stand-in for a real, connected stream (has a valid fileno)."""

    def fileno(self) -> int:
        return 3

    def write(self, _text: str) -> int:
        return 0


@_WINDOWS_ONLY
class ColorRefTest(unittest.TestCase):
    def test_byte_order_is_bgr(self) -> None:
        # #rrggbb -> 0x00bbggrr: the red byte is lowest, the blue byte highest.
        self.assertEqual(win32._colorref('#191918'), 0x181919)
        self.assertEqual(win32._colorref('#faf9f5'), 0xF5F9FA)

    def test_leading_hash_is_optional(self) -> None:
        self.assertEqual(win32._colorref('191918'), win32._colorref('#191918'))

    def test_channel_extremes(self) -> None:
        self.assertEqual(win32._colorref('#000000'), 0x000000)
        self.assertEqual(win32._colorref('#ffffff'), 0xFFFFFF)
        self.assertEqual(win32._colorref('#ff0000'), 0x0000FF)  # pure red lands in the low byte

    def test_malformed_returns_none(self) -> None:
        for bad in ('', None, '#fff', '#1234567', 'zzzzzz', '#gggggg'):
            self.assertIsNone(win32._colorref(bad))


@_WINDOWS_ONLY
class WindowBackgroundColorTest(unittest.TestCase):
    def _patch_registry(self, value: object | None, raises: bool = False):
        query = mock.Mock()
        if raises:
            query.side_effect = OSError('no such value')
        else:
            query.return_value = (value, 4)

        open_key = mock.MagicMock()
        open_key.return_value.__enter__.return_value = object()

        return mock.patch.multiple(win32.winreg, OpenKey=open_key, QueryValueEx=query)

    def test_light_theme(self) -> None:
        with self._patch_registry(1):
            self.assertEqual(win32.window_background_color(), '#faf9f5')

    def test_dark_theme(self) -> None:
        with self._patch_registry(0):
            self.assertEqual(win32.window_background_color(), '#191918')

    def test_missing_preference_falls_back_to_dark(self) -> None:
        with self._patch_registry(None, raises=True):
            self.assertEqual(win32.window_background_color(), '#191918')


@_WINDOWS_ONLY
class ApplyNativeBackgroundTest(unittest.TestCase):
    """The interop guards (no window, malformed colour) return before any .NET
    call; the interop itself only runs inside a live WebView2 host."""

    def test_none_native_is_a_no_op(self) -> None:
        self.assertFalse(win32.apply_native_background(None, '#191918'))

    def test_malformed_colour_is_a_no_op(self) -> None:
        self.assertFalse(win32.apply_native_background(object(), 'not-a-colour'))


@_WINDOWS_ONLY
class DotnetVersionTest(unittest.TestCase):
    def test_non_dword_release_does_not_crash(self) -> None:
        # A damaged registry can hold a non-DWORD Release; comparing it against
        # the integer thresholds must not raise an uncaught TypeError at startup.
        with mock.patch.object(win32.winreg, 'OpenKey'), \
             mock.patch.object(win32.winreg, 'QueryValueEx', return_value=('not-a-dword', 1)):
            self.assertEqual(win32._dotnet_version(), 'not found')

    def test_valid_release_maps_to_a_version(self) -> None:
        with mock.patch.object(win32.winreg, 'OpenKey'), \
             mock.patch.object(win32.winreg, 'QueryValueEx', return_value=(533320, 1)):
            self.assertEqual(win32._dotnet_version(), '4.8.1 (release 533320)')


@_WINDOWS_ONLY
class SetupConsoleTest(unittest.TestCase):
    def test_usable_streams_are_not_clobbered(self) -> None:
        # A console session or a redirected file must be left in place, or the
        # console buffer would swallow output meant for the redirect target.
        out, err = _FakeStream(), _FakeStream()
        with mock.patch.object(win32.sys, 'stdout', out), \
             mock.patch.object(win32.sys, 'stderr', err), \
             mock.patch.object(win32, '_kernel32') as kernel32, \
             mock.patch('builtins.open') as open_mock, \
             mock.patch.dict(os.environ, {}, clear=False):
            win32.setup_console()
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
            open_mock.assert_not_called()
            kernel32.AttachConsole.assert_not_called()
            kernel32.AllocConsole.assert_not_called()

    def test_missing_stream_is_bound_to_the_console(self) -> None:
        err = _FakeStream()
        with mock.patch.object(win32.sys, 'stdout', None), \
             mock.patch.object(win32.sys, 'stderr', err), \
             mock.patch.object(win32, '_kernel32') as kernel32, \
             mock.patch('builtins.open') as open_mock, \
             mock.patch.dict(os.environ, {}, clear=False):
            kernel32.AttachConsole.return_value = 1
            win32.setup_console()
            # Only the missing stdout is rebound; the usable stderr is untouched.
            open_mock.assert_called_once_with('CONOUT$', 'w', encoding='utf-8', errors='backslashreplace')
            self.assertIsNot(sys.stdout, None)
            self.assertIs(sys.stderr, err)


@_WINDOWS_ONLY
class CopyTextEncodingTest(unittest.TestCase):
    """A value that cannot be UTF-16 encoded must fail before touching the clipboard."""

    def test_lone_surrogate_returns_false_without_touching_the_clipboard(self) -> None:
        # A lone UTF-16 surrogate (which survives json.loads over the bridge)
        # cannot be encoded; copy_text must refuse before opening/emptying the
        # clipboard, so existing clipboard contents are not wiped by a failed copy.
        with mock.patch.object(win32._user32, 'OpenClipboard') as open_clip, \
             mock.patch.object(win32._user32, 'EmptyClipboard'):
            self.assertFalse(win32.copy_text('\ud800'))
            open_clip.assert_not_called()


@_WINDOWS_ONLY
class LaunchSurfaceTest(unittest.TestCase):
    """Both launch surfaces report a refusing shell rather than raising into the bridge."""

    def test_open_path_reports_a_failing_shell(self) -> None:
        with mock.patch.object(win32.os, 'startfile', side_effect=OSError):
            self.assertFalse(win32.open_path(r'C:\Windows'))

    def test_open_uri_reports_a_failing_shell(self) -> None:
        with mock.patch.object(win32.os, 'startfile', side_effect=OSError):
            self.assertFalse(win32.open_uri('vscode://x/y'))


@_WINDOWS_ONLY
class NoWindowKwargsTest(unittest.TestCase):
    def test_create_no_window_is_requested(self) -> None:
        # 0x08000000 is CREATE_NO_WINDOW; without it the one wsl.exe enumeration
        # would flash a console on a windowed build.
        self.assertEqual(win32.no_window_kwargs(), {'creationflags': 0x08000000})


if __name__ == '__main__':
    unittest.main()
