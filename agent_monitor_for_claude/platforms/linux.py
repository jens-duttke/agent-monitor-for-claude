"""
Linux Backend
==============

Linux implementations of the platform API, targeting a freedesktop desktop
session (GNOME, KDE, wlroots compositors); see
:mod:`agent_monitor_for_claude.platforms` for the dispatch and for what each
function promises.

``gi`` is imported lazily inside the functions that need it, so this module
stays importable in a plain virtual environment - the test suite depends on
that - and every helper degrades to a documented fallback when the session bus,
GTK, or the desktop portal is unavailable rather than raising.

Nothing here executes an external program.  The two launch surfaces the repo's
privacy statement lists go through GIO instead: :func:`open_path` and
:func:`open_uri` hand an already-validated target to
``Gio.AppInfo.launch_default_for_uri``, the desktop's own "open this" entry
point, and :func:`reveal_file` asks the session's file manager over D-Bus
(``org.freedesktop.FileManager1.ShowItems``) to show a file selected in its
folder - which opens no program for the file and never reads it.  Window
enumeration and activation live in :mod:`~agent_monitor_for_claude.platforms.x11`.
Beyond the application's own window background, nothing here writes.
"""
from __future__ import annotations

import os
import platform
import sys
import threading
from pathlib import Path
from typing import Any

from .x11 import activate_window, enum_windows

__all__ = [
    'DIAGNOSTIC_PACKAGES', 'activate_window', 'ask_yes_no', 'apply_native_background', 'copy_text', 'diagnostic_display_rows',
    'diagnostic_post_init_rows', 'diagnostic_runtime_rows', 'diagnostic_system_rows', 'enum_windows',
    'no_window_kwargs', 'open_path', 'open_uri', 'paint_window', 'prepare_gui_environment', 'reveal_file',
    'setup_console', 'show_error_box', 'storage_dir', 'window_background_color',
]

# Third-party packages worth reporting in the diagnostics output.  pythonnet and
# clr-loader belong to the Windows WebView2 host and are never installed here.
DIAGNOSTIC_PACKAGES = ('pywebview', 'PyGObject', 'psutil')

# Initial window colours, held identical to the content area's --bg in
# index.css so the bare window matches the content instead of flashing a
# mismatched colour.  The page itself picks the stored theme (or the system
# preference) before first paint.
_WINDOW_BACKGROUND_DARK = '#191918'
_WINDOW_BACKGROUND_LIGHT = '#faf9f5'

# Directory name for the browser profile, in the XDG data directory.
_STORAGE_DIR_NAME = 'agent-monitor-for-claude'

# A Snap-confined application rewrites part of the environment for the programs
# started from it, pointing them at its own bundled toolkit.  VS Code keeps each
# value it replaced under the original name plus this suffix, which is what makes
# undoing it exact rather than guesswork.
_SNAP_ORIGINAL_SUFFIX = '_VSCODE_SNAP_ORIG'

# Variables that name a toolkit module or data directory outright.  Any of these
# still pointing into a Snap after the restore above would make GTK load that
# Snap's modules, so they are dropped.  Deliberately not ``XDG_DATA_DIRS`` and
# friends: those mix a Snap entry into a list of legitimate ones, and dropping
# the whole list would take the system directories with it.
_SNAP_MODULE_VARIABLES = (
    'GTK_PATH', 'GTK_EXE_PREFIX', 'GIO_MODULE_DIR', 'GTK_IM_MODULE_FILE',
    'GDK_PIXBUF_MODULE_FILE', 'LOCPATH',
)

# Matched anywhere in the value, not as a prefix: a Snap's per-user directory
# lives under the home directory (``~/snap/<app>/...``), which is exactly where
# VS Code points ``GIO_MODULE_DIR``.  The trailing slash keeps an unrelated
# ``/snapshots/`` from matching.
_SNAP_PATH_MARKER = '/snap/'

# XDG desktop portal: the cross-desktop reading of the light/dark preference,
# which works under both X11 and Wayland and on GNOME as well as KDE.
_PORTAL_SERVICE = ('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
_APPEARANCE_NAMESPACE = 'org.freedesktop.appearance'
_COLOR_SCHEME_KEY = 'color-scheme'
_COLOR_SCHEME_LIGHT = 2  # 0 = no preference, 1 = prefer dark, 2 = prefer light

# The freedesktop interface every mainstream file manager implements for
# "show this file, selected, in its folder".
_FILE_MANAGER_SERVICE = ('org.freedesktop.FileManager1', '/org/freedesktop/FileManager1')

_DBUS_TIMEOUT_MS = 2000

# How long a call marshalled onto the GTK main loop is waited for.  Reaching it
# means no main loop is running (the app has not started its window yet, or the
# call came from a test), which is reported as a plain failure.
_MAIN_LOOP_TIMEOUT = 2.0

# Cached proxies; a failed lookup is retried, an unavailable ``gi`` is not.
_proxies: dict[tuple[tuple[str, str], str | None], Any] = {}
_gi_available: bool | None = None

# The CSS provider currently carrying the window background, kept so a theme
# switch can take the previous one off the widgets again.  A style context
# accumulates providers, so adding one per switch would leave a growing stack of
# dead rules behind - the newest wins, so it would look right and still grow.
_background_provider: Any = None


def show_error_box(message: str, title: str) -> None:
    """Show a modal error dialog, falling back to stderr without a usable display."""
    if not _try_gtk_dialog(message, title):
        print(f'{title}: {message}', file=sys.stderr, flush=True)


def ask_yes_no(message: str, title: str) -> bool:
    """Ask a yes/no question in a modal, always-on-top dialog.

    Without a reachable display there is nobody to answer, so the question is
    declined - the safe direction, since the caller uses "yes" to terminate the
    instance that is already running.
    """
    gi = _import_gi()
    if gi is None:
        return False

    try:
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return False

    if not Gtk.init_check()[0]:
        return False

    dialog = Gtk.MessageDialog(
        transient_for=None, modal=True, message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO, text=title, secondary_text=message,
    )
    dialog.set_title(title)
    dialog.set_keep_above(True)
    response = dialog.run()
    dialog.destroy()

    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    return response == Gtk.ResponseType.YES


def no_window_kwargs() -> dict[str, Any]:
    """Return ``subprocess`` keyword arguments that suppress a console window.

    POSIX has no console-window concept, so no flags are needed.
    """
    return {}


def prepare_gui_environment() -> None:
    """Prepare this process's environment for the GUI toolkit.

    Idempotent, and must run before anything touches GTK - the instance guard's
    dialog included.  Two things are settled here:

    *Leaving a Snap's environment behind.*  See :func:`_leave_snap_environment`.

    *The DMA-BUF renderer.*  WebKitGTK's produces a blank window on several
    drivers (NVIDIA's proprietary stack, and inside VMs), and the failure is
    silent - the window opens, the page loads, and nothing is drawn.  Disabling
    it falls back to a path that works everywhere at a small cost in compositing
    performance, which is the right trade for a monitor window.  ``setdefault``
    leaves an explicit choice by the user alone.
    """
    _leave_snap_environment()

    os.environ.setdefault('WEBKIT_DISABLE_DMABUF_RENDERER', '1')


def _leave_snap_environment() -> None:
    """Undo the environment a Snap-confined launcher imposed on this process.

    A terminal inside a Snap-packaged application - VS Code from the Snap Store,
    which is where a Claude Code user is most likely to start this - hands every
    program it launches an environment pointing at the Snap's own GTK and GIO
    modules.  Loading one of those into a system Python is fatal: the module
    carries an ``RPATH`` into the Snap's runtime, so ``dlopen`` pulls in that
    runtime's C library and the process dies at the first symbol it cannot
    resolve (``symbol lookup error: ... libpthread.so.0``), before any of this
    application's code can report anything.  The same environment redirects
    ``XDG_DATA_HOME`` into the Snap, which is where the interface preferences
    would otherwise be stored.

    Undoing it is exact rather than heuristic: the launcher keeps each value it
    replaced under ``<NAME>_VSCODE_SNAP_ORIG``, so the original is restored from
    there, and an empty one means the variable was unset to begin with.  A
    module variable still naming a path inside a Snap afterwards is dropped, so
    a launcher that offers no such record cannot leave one behind either.

    This runs unconditionally, and deliberately does not first ask whether it is
    "inside a Snap": ``SNAP`` is inherited by every descendant of a confined
    process, so a terminal opened in one has it set while the programs started
    from there - this application among them - are ordinary unconfined system
    processes for which those paths are simply wrong.  There is no case where
    this application wants them: it is never itself packaged as a Snap.
    """
    for name in [key for key in os.environ if key.endswith(_SNAP_ORIGINAL_SUFFIX)]:
        original = os.environ.pop(name)
        variable = name[:-len(_SNAP_ORIGINAL_SUFFIX)]
        if not variable:
            # A variable named nothing but the suffix records nothing; assigning
            # the empty name would raise, and this runs before the window opens.
            continue

        if original:
            os.environ[variable] = original
        else:
            os.environ.pop(variable, None)

    for variable in _SNAP_MODULE_VARIABLES:
        if _SNAP_PATH_MARKER in os.environ.get(variable, ''):
            del os.environ[variable]


def storage_dir() -> Path:
    """Return the browser profile directory used for UI preference storage."""
    base = os.environ.get('XDG_DATA_HOME')
    root = Path(base) if base else Path.home() / '.local' / 'share'
    return root / _STORAGE_DIR_NAME


def copy_text(text: str) -> bool:
    """Place *text* on the clipboard; return True on success.

    On X11 and Wayland alike the clipboard belongs to a live application with a
    running event loop, so the write is marshalled onto the GTK main loop the
    application's window already runs, and ``store()`` hands the content to the
    session's clipboard manager so it survives this process exiting.  Without a
    running main loop (no window yet) the call cannot be served and reports
    False rather than blocking.
    """
    gi = _import_gi()
    if gi is None:
        return False

    try:
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError):
        return False

    done = threading.Event()
    stored = False

    def _assign() -> bool:
        nonlocal stored
        try:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(text, -1)
            clipboard.store()
            stored = True
        except Exception:  # noqa: BLE001  # a clipboard the display refuses must not raise into the bridge
            stored = False
        finally:
            done.set()

        return False

    GLib.idle_add(_assign)
    done.wait(_MAIN_LOOP_TIMEOUT)

    return stored


def open_path(path: str) -> bool:
    """Open an already-validated directory in the desktop's file manager."""
    gi = _import_gi()
    if gi is None:
        return False

    from gi.repository import Gio

    return _launch_uri(Gio.File.new_for_path(path).get_uri())


def open_uri(uri: str) -> bool:
    """Hand an already-validated URI to its registered handler."""
    if _import_gi() is None:
        return False

    return _launch_uri(uri)


def reveal_file(path: str) -> bool:
    """Ask the session's file manager to show *path* selected in its folder.

    Uses the freedesktop ``org.freedesktop.FileManager1.ShowItems`` interface,
    which raises a file-manager window with the item selected - no program is
    launched for the file and its content is never read.  A session with no such
    service (a minimal desktop, a file manager that does not implement it)
    reports False, leaving the caller to fall back on the plain folder.
    """
    gi = _import_gi()
    if gi is None:
        return False

    proxy = _session_proxy(_FILE_MANAGER_SERVICE)
    if proxy is None:
        return False

    from gi.repository import Gio, GLib

    uri = Gio.File.new_for_path(path).get_uri()
    try:
        proxy.call_sync(
            'ShowItems', GLib.Variant('(ass)', ([uri], '')), Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MS, None,
        )
    except GLib.Error:
        return False

    return True


def window_background_color() -> str:
    """Return the content-area background colour matching the desktop's colour scheme.

    Reads ``color-scheme`` from the XDG desktop portal, which answers under both
    X11 and Wayland and on every mainstream desktop.  An unreachable portal, or
    one reporting no preference, yields the dark colour - the page then reports
    its real colour back over the bridge as soon as it has resolved its own
    stored theme, so this only has to be a good guess for the first frame.
    """
    return _WINDOW_BACKGROUND_LIGHT if _read_color_scheme() == _COLOR_SCHEME_LIGHT else _WINDOW_BACKGROUND_DARK


def paint_window(handle: int, hex_color: str) -> bool:
    """Return False - a Linux toolkit owns its window's background.

    On Windows the window *class* carries a background brush that the system
    paints newly exposed area with during a resize, before the application gets
    to draw.  GTK has no such per-class brush: the widget's own CSS background
    is what fills that area, and :func:`apply_native_background` already sets it.
    """
    return False


def apply_native_background(native: object, hex_color: str) -> bool:
    """Recolour a live GTK/WebKit window to *hex_color*.

    Sets the CSS background of the GTK window and of the scrolled container
    pywebview puts the web view in, and the web view's own background colour -
    the three surfaces that can show through while WebKit has not caught up with
    a resize.  The assignment is marshalled onto the GTK main loop, since the
    bridge call arrives on a worker thread and GTK is single-threaded.

    Parameters
    ----------
    native : object
        pywebview's native window (the ``Gtk.ApplicationWindow``).
    hex_color : str
        Target colour as ``#rrggbb``.

    Returns
    -------
    bool
        True if the colour was valid and applied.  Best-effort otherwise: any
        failure (no GTK, no running main loop, a window shape this does not
        recognise) is swallowed and reported as False.
    """
    gi = _import_gi()
    if native is None or gi is None or not _is_hex_color(hex_color):
        return False

    try:
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError):
        return False

    done = threading.Event()
    applied = False

    def _assign() -> bool:
        global _background_provider
        nonlocal applied
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(f'* {{ background-color: {hex_color}; }}'.encode('utf-8'))

            rgba = Gdk.RGBA()
            rgba.parse(hex_color)

            for widget in _background_widgets(native):
                context = widget.get_style_context()
                if _background_provider is not None:
                    context.remove_provider(_background_provider)
                context.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            _background_provider = provider

            web_view = _web_view(native)
            if web_view is not None:
                web_view.set_background_color(rgba)

            applied = True
        except Exception:  # noqa: BLE001  # a toolkit failure must not raise into the bridge
            applied = False
        finally:
            done.set()

        return False

    GLib.idle_add(_assign)
    done.wait(_MAIN_LOOP_TIMEOUT)

    return applied


def setup_console() -> None:
    """Ensure diagnostics have somewhere to print.

    Nothing to do: a Linux process is started from a terminal or a desktop
    launcher with its standard streams already connected, and a redirect the
    user set up (``--verbose > diag.txt``) must be left exactly as it is.
    """


def diagnostic_system_rows() -> list[tuple[str, str]]:
    """Return the ``System`` diagnostics rows."""
    return [
        ('OS', f'{_os_release_name()} ({platform.system()} {platform.release()})'),
        ('Architecture', platform.machine()),
    ]


def diagnostic_display_rows() -> list[tuple[str, str]]:
    """Return the ``Display`` diagnostics rows (session type and the two display sockets)."""
    return [
        ('Session type', os.environ.get('XDG_SESSION_TYPE') or 'unknown'),
        ('Desktop', os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown'),
        ('DISPLAY', os.environ.get('DISPLAY') or '(not set)'),
        ('WAYLAND_DISPLAY', os.environ.get('WAYLAND_DISPLAY') or '(not set)'),
    ]


def diagnostic_runtime_rows() -> list[tuple[str, str]]:
    """Return the ``Runtimes`` diagnostics rows (GTK and WebKitGTK)."""
    return [('GTK', _gtk_version()), ('WebKitGTK', _webkit_version())]


def diagnostic_post_init_rows() -> list[tuple[str, str]]:
    """Return diagnostics rows only available once the GTK host has loaded."""
    return []


def _launch_uri(uri: str) -> bool:
    """Hand *uri* to the desktop's default handler through GIO."""
    from gi.repository import Gio, GLib

    try:
        return bool(Gio.AppInfo.launch_default_for_uri(uri, None))
    except GLib.Error:
        return False


def _background_widgets(native: object) -> list[Any]:
    """Return the widgets whose background can show through: the window and its container."""
    widgets = [native]

    child = getattr(native, 'get_child', None)
    container = child() if callable(child) else None
    if container is not None:
        widgets.append(container)

    return widgets


def _web_view(native: object) -> Any:
    """Return the ``WebKit.WebView`` inside pywebview's GTK window, or None.

    pywebview nests it one level down, inside a ``Gtk.ScrolledWindow``.  Anything
    that does not have that shape yields ``None`` rather than a wrong widget -
    the window and container backgrounds are already set either way.
    """
    child = getattr(native, 'get_child', None)
    container = child() if callable(child) else None
    if container is None:
        return None

    inner = getattr(container, 'get_child', None)
    view = inner() if callable(inner) else None

    return view if hasattr(view, 'set_background_color') else None


def _is_hex_color(hex_color: str) -> bool:
    """Return whether *hex_color* is a ``#rrggbb`` string."""
    value = (hex_color or '').lstrip('#')
    if len(value) != 6:
        return False

    try:
        int(value, 16)
    except ValueError:
        return False

    return True


def _import_gi() -> Any:
    """Return the ``gi`` module, or None when PyGObject is unavailable."""
    global _gi_available
    if _gi_available is False:
        return None

    try:
        import gi
    except ImportError:
        _gi_available = False
        return None

    _gi_available = True

    return gi


def _session_proxy(service: tuple[str, str], interface: str | None = None) -> Any:
    """Return a cached D-Bus proxy for *service*, or None when unavailable.

    *interface* defaults to the bus name, which holds for the file-manager
    service; the portal exposes its settings under a different interface name.
    """
    cache_key = (service, interface)
    if cache_key in _proxies:
        return _proxies[cache_key]

    if _import_gi() is None:
        return None

    from gi.repository import Gio, GLib

    name, path = service
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, name, path, interface or name, None,
        )
    except GLib.Error:
        return None

    _proxies[cache_key] = proxy

    return proxy


def _read_color_scheme() -> int | None:
    """Return the portal's ``color-scheme`` value, or None when unavailable."""
    proxy = _session_proxy(_PORTAL_SERVICE, 'org.freedesktop.portal.Settings')
    if proxy is None:
        return None

    from gi.repository import Gio, GLib

    try:
        result = proxy.call_sync(
            'Read', GLib.Variant('(ss)', (_APPEARANCE_NAMESPACE, _COLOR_SCHEME_KEY)),
            Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MS, None,
        )
    except GLib.Error:
        return None

    unpacked = result.unpack()

    return unpacked[0] if unpacked else None


def _try_gtk_dialog(message: str, title: str) -> bool:
    """Show a modal GTK error dialog.  Returns False when GTK is unavailable."""
    gi = _import_gi()
    if gi is None:
        return False

    try:
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return False

    if not Gtk.init_check()[0]:
        return False

    dialog = Gtk.MessageDialog(
        transient_for=None, modal=True, message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK, text=title, secondary_text=message,
    )
    dialog.set_title(title)
    dialog.set_keep_above(True)
    dialog.run()
    dialog.destroy()

    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    return True


def _os_release_name() -> str:
    """Return the distribution's pretty name from ``/etc/os-release``, or 'unknown'."""
    try:
        release = platform.freedesktop_os_release()
    except (OSError, AttributeError):
        return 'unknown'

    return release.get('PRETTY_NAME') or release.get('NAME') or 'unknown'


def _gtk_version() -> str:
    """Return the running GTK version, or 'not found'."""
    gi = _import_gi()
    if gi is None:
        return 'not found'

    try:
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk

        return f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}'
    except (ImportError, ValueError):
        return 'not found'


def _webkit_version() -> str:
    """Return the installed WebKitGTK version, or 'not found'.

    The GTK-3 bindings ship as ``WebKit2`` (API 4.1, or the older 4.0); the GTK-4
    ones as ``WebKit`` 6.0.  Each candidate is tried in turn, newest first, with
    a plain import per candidate - the repo forbids dynamic imports, and there
    are only three names to spell out.
    """
    gi = _import_gi()
    if gi is None:
        return 'not found'

    for version in ('4.1', '4.0'):
        try:
            gi.require_version('WebKit2', version)
            from gi.repository import WebKit2

            return _version_triple(WebKit2)
        except (ImportError, ValueError):
            continue

    try:
        gi.require_version('WebKit', '6.0')
        from gi.repository import WebKit

        return _version_triple(WebKit)
    except (ImportError, ValueError):
        return 'not found'


def _version_triple(module: Any) -> str:
    """Format a GObject-introspection module's major/minor/micro version."""
    return f'{module.get_major_version()}.{module.get_minor_version()}.{module.get_micro_version()}'
