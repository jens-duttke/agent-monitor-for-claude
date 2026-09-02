"""
Tests for the Linux platform backend.

Skipped as a whole on Windows.  The GTK and D-Bus calls themselves need a live
desktop session, so what is covered here is everything around them: the
decisions the module makes before it reaches a toolkit, and that every entry
point degrades to a documented answer when the toolkit is not there.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_LINUX_ONLY = unittest.skipIf(sys.platform == 'win32', 'Linux backend')

if sys.platform != 'win32':
    from agent_monitor_for_claude.platforms import linux
else:  # pragma: no cover - the Linux backend is not imported on Windows
    linux = None


@_LINUX_ONLY
class StorageDirTest(unittest.TestCase):
    def test_follows_xdg_data_home(self) -> None:
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': '/custom/data'}):
            self.assertEqual(linux.storage_dir(), Path('/custom/data/agent-monitor-for-claude'))

    def test_falls_back_to_the_default_data_directory(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(linux.Path, 'home', return_value=Path('/home/dev')):
            self.assertEqual(linux.storage_dir(), Path('/home/dev/.local/share/agent-monitor-for-claude'))


@_LINUX_ONLY
class WindowBackgroundColorTest(unittest.TestCase):
    """The portal reports a colour scheme; anything else must read as dark."""

    def _with_scheme(self, value: object):
        return mock.patch.object(linux, '_read_color_scheme', return_value=value)

    def test_light_preference(self) -> None:
        with self._with_scheme(2):
            self.assertEqual(linux.window_background_color(), '#faf9f5')

    def test_dark_preference(self) -> None:
        with self._with_scheme(1):
            self.assertEqual(linux.window_background_color(), '#191918')

    def test_no_preference_reads_as_dark(self) -> None:
        with self._with_scheme(0):
            self.assertEqual(linux.window_background_color(), '#191918')

    def test_unreachable_portal_reads_as_dark(self) -> None:
        with self._with_scheme(None):
            self.assertEqual(linux.window_background_color(), '#191918')


@_LINUX_ONLY
class HexColorTest(unittest.TestCase):
    def test_accepts_both_spellings(self) -> None:
        self.assertTrue(linux._is_hex_color('#191918'))
        self.assertTrue(linux._is_hex_color('191918'))

    def test_rejects_malformed(self) -> None:
        for bad in ('', None, '#fff', '#1234567', 'zzzzzz', '#gggggg'):
            self.assertFalse(linux._is_hex_color(bad))


@_LINUX_ONLY
class PaintWindowTest(unittest.TestCase):
    def test_reports_the_missing_capability_rather_than_pretending(self) -> None:
        # GTK has no per-class background brush to point anywhere; the window's
        # own CSS background is what apply_native_background sets instead.
        self.assertFalse(linux.paint_window(1234, '#191918'))


@_LINUX_ONLY
class ApplyNativeBackgroundTest(unittest.TestCase):
    def test_none_native_is_a_no_op(self) -> None:
        self.assertFalse(linux.apply_native_background(None, '#191918'))

    def test_malformed_colour_is_a_no_op(self) -> None:
        self.assertFalse(linux.apply_native_background(object(), 'not-a-colour'))

    def test_without_pygobject_it_reports_false(self) -> None:
        with mock.patch.object(linux, '_import_gi', return_value=None):
            self.assertFalse(linux.apply_native_background(object(), '#191918'))


@_LINUX_ONLY
class WidgetShapeTest(unittest.TestCase):
    """pywebview nests the web view inside a scrolled window; anything else must not be mistaken for it."""

    def test_web_view_is_found_one_level_down(self) -> None:
        view = mock.Mock(spec=['set_background_color'])
        container = mock.Mock(spec=['get_child'])
        container.get_child.return_value = view
        window = mock.Mock(spec=['get_child'])
        window.get_child.return_value = container

        self.assertIs(linux._web_view(window), view)
        self.assertEqual(linux._background_widgets(window), [window, container])

    def test_an_unexpected_shape_yields_no_view(self) -> None:
        window = mock.Mock(spec=[])
        self.assertIsNone(linux._web_view(window))
        self.assertEqual(linux._background_widgets(window), [window])

    def test_a_child_without_the_background_method_is_not_used(self) -> None:
        container = mock.Mock(spec=['get_child'])
        container.get_child.return_value = mock.Mock(spec=[])
        window = mock.Mock(spec=['get_child'])
        window.get_child.return_value = container

        self.assertIsNone(linux._web_view(window))


@_LINUX_ONLY
class LaunchSurfaceTest(unittest.TestCase):
    def test_open_path_reports_false_without_pygobject(self) -> None:
        with mock.patch.object(linux, '_import_gi', return_value=None):
            self.assertFalse(linux.open_path('/tmp'))

    def test_open_uri_reports_false_without_pygobject(self) -> None:
        with mock.patch.object(linux, '_import_gi', return_value=None):
            self.assertFalse(linux.open_uri('vscode://x/y'))

    def test_reveal_file_reports_false_without_a_file_manager_service(self) -> None:
        # A desktop with no org.freedesktop.FileManager1 must report the miss, so
        # window_focus falls back to opening the containing folder.
        with mock.patch.object(linux, '_session_proxy', return_value=None):
            self.assertFalse(linux.reveal_file('/tmp/x.jsonl'))


@_LINUX_ONLY
class ClipboardTest(unittest.TestCase):
    def test_reports_false_without_pygobject(self) -> None:
        with mock.patch.object(linux, '_import_gi', return_value=None):
            self.assertFalse(linux.copy_text('session-id'))


@_LINUX_ONLY
class EnvironmentTest(unittest.TestCase):
    def test_no_window_kwargs_is_empty(self) -> None:
        # POSIX has no console-window concept, so subprocess needs no flags.
        self.assertEqual(linux.no_window_kwargs(), {})

    def test_prepare_gui_environment_disables_the_dmabuf_renderer(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            linux.prepare_gui_environment()
            self.assertEqual(os.environ['WEBKIT_DISABLE_DMABUF_RENDERER'], '1')

    def test_an_explicit_choice_is_left_alone(self) -> None:
        with mock.patch.dict(os.environ, {'WEBKIT_DISABLE_DMABUF_RENDERER': '0'}):
            linux.prepare_gui_environment()
            self.assertEqual(os.environ['WEBKIT_DISABLE_DMABUF_RENDERER'], '0')

    def test_setup_console_leaves_the_streams_alone(self) -> None:
        # A Linux process is started with its streams already connected, and a
        # redirect the user set up must survive untouched.
        out, err = sys.stdout, sys.stderr
        linux.setup_console()
        self.assertIs(sys.stdout, out)
        self.assertIs(sys.stderr, err)


@_LINUX_ONLY
class LeaveSnapEnvironmentTest(unittest.TestCase):
    """A terminal inside a Snap hands us its toolkit; loading it would kill the process."""

    _SNAP_GTK = '/snap/code/259/usr/lib/x86_64-linux-gnu/gtk-3.0'

    def test_a_recorded_original_is_put_back(self) -> None:
        with mock.patch.dict(os.environ, {'XDG_DATA_DIRS': '/snap/code/259/usr/share:/usr/share',
                                          'XDG_DATA_DIRS_VSCODE_SNAP_ORIG': '/usr/share'}, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(os.environ['XDG_DATA_DIRS'], '/usr/share')

    def test_an_empty_record_means_the_variable_was_unset(self) -> None:
        with mock.patch.dict(os.environ, {'GTK_PATH': self._SNAP_GTK, 'GTK_PATH_VSCODE_SNAP_ORIG': ''}, clear=True):
            linux._leave_snap_environment()
            self.assertNotIn('GTK_PATH', os.environ)

    def test_the_bookkeeping_variables_are_removed_too(self) -> None:
        with mock.patch.dict(os.environ, {'LOCPATH': '/snap/x/locale', 'LOCPATH_VSCODE_SNAP_ORIG': ''}, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(dict(os.environ), {})

    def test_a_module_path_into_a_snap_is_dropped_without_a_record(self) -> None:
        # A launcher that keeps no record of what it replaced must not be able to
        # leave a Snap module directory behind either.
        with mock.patch.dict(os.environ, {'GIO_MODULE_DIR': '/home/dev/snap/code/common/.cache/gio-modules'}, clear=True):
            linux._leave_snap_environment()
            self.assertNotIn('GIO_MODULE_DIR', os.environ)

    def test_a_module_path_outside_a_snap_is_left_alone(self) -> None:
        with mock.patch.dict(os.environ, {'GTK_PATH': '/usr/lib/x86_64-linux-gnu/gtk-3.0'}, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(os.environ['GTK_PATH'], '/usr/lib/x86_64-linux-gnu/gtk-3.0')

    def test_a_mixed_list_is_never_dropped_wholesale(self) -> None:
        # XDG_DATA_DIRS mixes a Snap entry into legitimate ones; dropping the
        # whole list would take the system directories with it.  Only a recorded
        # original may replace it.
        mixed = '/snap/code/259/usr/share:/usr/share'
        with mock.patch.dict(os.environ, {'XDG_DATA_DIRS': mixed}, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(os.environ['XDG_DATA_DIRS'], mixed)

    def test_it_runs_even_when_snap_is_set(self) -> None:
        # SNAP is inherited by every descendant of a confined process, so a
        # terminal opened in one has it set while the programs started from
        # there are ordinary unconfined processes.
        with mock.patch.dict(os.environ, {'SNAP': '/snap/code/259', 'GTK_PATH': self._SNAP_GTK}, clear=True):
            linux._leave_snap_environment()
            self.assertNotIn('GTK_PATH', os.environ)

    def test_a_record_naming_no_variable_is_skipped(self) -> None:
        # Assigning the empty name raises, and this runs before the window opens.
        with mock.patch.dict(os.environ, {'_VSCODE_SNAP_ORIG': '/some/value'}, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(dict(os.environ), {})

    def test_a_per_user_snap_directory_is_recognised(self) -> None:
        # A Snap's per-user tree lives under the home directory, so the match has
        # to look anywhere in the value rather than only at its start.
        with mock.patch.dict(os.environ, {'GIO_MODULE_DIR': '/home/dev/snap/code/common/.cache/gio-modules',
                                          'GTK_PATH': '/home/dev/snapshots/gtk-3.0'}, clear=True):
            linux._leave_snap_environment()
            self.assertNotIn('GIO_MODULE_DIR', os.environ)
            self.assertEqual(os.environ['GTK_PATH'], '/home/dev/snapshots/gtk-3.0')

    def test_it_is_idempotent(self) -> None:
        with mock.patch.dict(os.environ, {'GTK_PATH': self._SNAP_GTK, 'GTK_PATH_VSCODE_SNAP_ORIG': ''}, clear=True):
            linux._leave_snap_environment()
            first = dict(os.environ)
            linux._leave_snap_environment()
            self.assertEqual(dict(os.environ), first)

    def test_a_clean_environment_is_untouched(self) -> None:
        clean = {'HOME': '/home/dev', 'GTK_PATH': '/usr/lib/gtk-3.0'}
        with mock.patch.dict(os.environ, clean, clear=True):
            linux._leave_snap_environment()
            self.assertEqual(dict(os.environ), clean)


@_LINUX_ONLY
class DiagnosticsTest(unittest.TestCase):
    def test_every_section_reports_rows_of_pairs(self) -> None:
        for rows in (linux.diagnostic_system_rows(), linux.diagnostic_display_rows(), linux.diagnostic_runtime_rows()):
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(len(row), 2)
                self.assertIsInstance(row[0], str)
                self.assertIsInstance(row[1], str)

    def test_unreadable_os_release_degrades(self) -> None:
        with mock.patch.object(linux.platform, 'freedesktop_os_release', side_effect=OSError):
            self.assertEqual(linux._os_release_name(), 'unknown')

    def test_toolkit_versions_report_not_found_without_pygobject(self) -> None:
        with mock.patch.object(linux, '_import_gi', return_value=None):
            self.assertEqual(linux._gtk_version(), 'not found')
            self.assertEqual(linux._webkit_version(), 'not found')


@_LINUX_ONLY
class ImportWithoutPyGObjectTest(unittest.TestCase):
    """The backend must import with no GUI toolkit present, or the test suite could not run.

    Staged in a subprocess rather than with a mock: this machine has PyGObject
    installed (the app needs it to run at all), so a mock would only prove the
    mock.  Blocking ``gi`` in a fresh interpreter proves the real import path.
    """

    def test_importing_the_backend_without_gi_still_works(self) -> None:
        script = (
            'import sys\n'
            "sys.modules['gi'] = None\n"
            'from agent_monitor_for_claude.platforms import linux\n'
            'assert linux._import_gi() is None\n'
            "assert linux.window_background_color() == '#191918'\n"
            "assert linux.copy_text('x') is False\n"
            'print("ok")\n'
        )
        result = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent), check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
