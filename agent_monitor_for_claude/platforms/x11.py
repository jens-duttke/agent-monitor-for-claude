"""
X11 Window Access
==================

Enumerates and activates top-level windows on an X server, so clicking a
session can bring its editor or terminal to the foreground on Linux the same
way it does on Windows.  Only the standard EWMH properties are read - the
window list, each window's owning pid, and each window's title - and the only
thing ever written is the ``_NET_ACTIVE_WINDOW`` request that asks the window
manager to raise one of them, on an explicit user click.

``libX11`` is loaded through ``ctypes`` rather than a binding, so no dependency
is added for it, and every entry point degrades: without a reachable display
(no ``DISPLAY``, no X server, a library that will not load) the enumeration
returns an empty list and activation returns ``False``.

A Wayland session reaches this through XWayland, which is where the editors and
terminals that matter here usually live.  A window drawn by a *native* Wayland
client is deliberately out of reach: the protocol has no equivalent of the EWMH
window list, and none of the compositors expose one to an unprivileged client -
so such a window is simply absent from the list rather than approximated.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os

__all__ = ['activate_window', 'enum_windows']

# X protocol constants: the predefined atoms this module asks for, the
# "any type" wildcard, the ClientMessage event type, and the two event masks a
# root-window message needs to reach the window manager.
_ANY_PROPERTY_TYPE = 0
_XA_CARDINAL = 6
_XA_WINDOW = 33
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
_SUBSTRUCTURE_REDIRECT_MASK = 1 << 20

# Property reads are capped rather than looped: a window list far beyond this is
# a broken server, not a desktop this feature can help with.
_MAX_PROPERTY_LONGS = 4096

# The XEvent union is declared as 24 longs; a ClientMessage must be sent in a
# buffer of that size or the server reads past the structure.
_XEVENT_LONGS = 24

# Source indication in _NET_ACTIVE_WINDOW: 1 = a normal application, 2 = a pager.
# An application asking on behalf of a user click is exactly case 1.
_SOURCE_APPLICATION = 1


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ('type', ctypes.c_int),
        ('serial', ctypes.c_ulong),
        ('send_event', ctypes.c_int),
        ('display', ctypes.c_void_p),
        ('window', ctypes.c_ulong),
        ('message_type', ctypes.c_ulong),
        ('format', ctypes.c_int),
        ('data', ctypes.c_long * 5),
    ]


def enum_windows() -> list[tuple[int, int, str]]:
    """Return the managed top-level windows as ``(window_id, pid, title)``, topmost first.

    Reads the window manager's client list and, per window, ``_NET_WM_PID`` and
    ``_NET_WM_NAME`` (falling back to ``WM_NAME``).  Windows with no title or no
    owning pid are dropped, matching what the Windows enumeration returns.

    The order matters as much as the contents: the caller falls back to "the
    first window this process owns" when no title matches, and on Windows that
    is the topmost one, because ``EnumWindows`` walks the z-order.  So
    ``_NET_CLIENT_LIST_STACKING`` is read first and reversed - the EWMH spec
    defines it bottom-to-top - and only a window manager that does not publish
    it falls back to ``_NET_CLIENT_LIST``, which is mapping order and would
    raise a session's *oldest* window instead of the one last used.

    Returns
    -------
    list[tuple[int, int, str]]
        Empty when no display is reachable, when ``libX11`` cannot be loaded, or
        when the window manager publishes no client list at all.
    """
    library = _library()
    if library is None:
        return []

    display = _open_display(library)
    if display is None:
        return []

    try:
        windows: list[tuple[int, int, str]] = []
        for window in _client_list(library, display):
            pid = _window_pid(library, display, window)
            title = _window_title(library, display, window)
            if pid is None or not title:
                continue

            windows.append((window, pid, title))

        return windows
    finally:
        library.XCloseDisplay(display)


def activate_window(window_id: int) -> bool:
    """Ask the window manager to raise and focus *window_id*.

    Sends the EWMH ``_NET_ACTIVE_WINDOW`` message to the root window, which is the
    request every compliant window manager honours - it also un-minimizes a window
    that is iconified, so no separate restore step is needed.  ``XRaiseWindow`` is
    issued alongside it for a bare or non-compliant window manager that ignores
    the message.

    Returns
    -------
    bool
        True once the request has been flushed to the server.  A window manager
        may still decline to focus it (focus-stealing prevention), which the
        server does not report back, so this is "asked", not "confirmed".
    """
    library = _library()
    if library is None:
        return False

    display = _open_display(library)
    if display is None:
        return False

    try:
        active_window = library.XInternAtom(display, b'_NET_ACTIVE_WINDOW', True)
        if not active_window:
            return False

        root = library.XDefaultRootWindow(display)

        buffer = (ctypes.c_long * _XEVENT_LONGS)()
        event = ctypes.cast(buffer, ctypes.POINTER(_XClientMessageEvent)).contents
        event.type = _CLIENT_MESSAGE
        event.window = window_id
        event.message_type = active_window
        event.format = 32
        event.data[0] = _SOURCE_APPLICATION
        event.data[1] = 0  # CurrentTime: no click timestamp is available here.

        mask = _SUBSTRUCTURE_NOTIFY_MASK | _SUBSTRUCTURE_REDIRECT_MASK
        sent = library.XSendEvent(display, root, False, mask, buffer)
        library.XRaiseWindow(display, window_id)
        library.XFlush(display)

        return bool(sent)
    finally:
        library.XCloseDisplay(display)


_library_cache: ctypes.CDLL | None = None
_library_loaded = False


def _library() -> ctypes.CDLL | None:
    """Return the prototyped ``libX11`` handle, or None when it cannot be loaded."""
    global _library_cache, _library_loaded

    if _library_loaded:
        return _library_cache

    _library_loaded = True

    name = ctypes.util.find_library('X11') or 'libX11.so.6'
    try:
        library = ctypes.CDLL(name)
        _declare(library)
    except (OSError, AttributeError):
        # No library, or one missing an entry point this module needs: report it
        # unavailable rather than raising out of a bridge call.
        return None

    _library_cache = library
    return library


def _declare(library: ctypes.CDLL) -> None:
    """Give every entry point an explicit prototype.

    Without them ctypes marshals through the default C ``int``, which truncates
    the 64-bit display pointer and every window and atom id on a 64-bit host.
    """
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XCloseDisplay.argtypes = [ctypes.c_void_p]
    library.XCloseDisplay.restype = ctypes.c_int
    library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    library.XInternAtom.restype = ctypes.c_ulong
    library.XFree.argtypes = [ctypes.c_void_p]
    library.XFree.restype = ctypes.c_int
    library.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    library.XRaiseWindow.restype = ctypes.c_int
    library.XFlush.argtypes = [ctypes.c_void_p]
    library.XFlush.restype = ctypes.c_int
    library.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
    library.XSendEvent.restype = ctypes.c_int
    library.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    library.XGetWindowProperty.restype = ctypes.c_int


def _client_list(library: ctypes.CDLL, display: int) -> list[int]:
    """Return the managed windows, topmost first; empty when neither list is published."""
    root = library.XDefaultRootWindow(display)

    stacking = library.XInternAtom(display, b'_NET_CLIENT_LIST_STACKING', True)
    if stacking:
        windows = _read_longs(library, display, root, stacking, _XA_WINDOW)
        if windows:
            return list(reversed(windows))

    mapping_order = library.XInternAtom(display, b'_NET_CLIENT_LIST', True)
    if not mapping_order:
        return []

    return _read_longs(library, display, root, mapping_order, _XA_WINDOW)


def _open_display(library: ctypes.CDLL) -> int | None:
    """Open the display named by ``DISPLAY``, or None when there is none to open."""
    if not os.environ.get('DISPLAY'):
        return None

    display = library.XOpenDisplay(None)
    return display or None


def _read_property(
    library: ctypes.CDLL, display: int, window: int, prop: int, req_type: int,
) -> tuple[bytes, int, int] | None:
    """Read one window property as ``(raw_bytes, format, item_count)``, or None.

    The raw buffer is copied out and freed before returning, so no caller ever
    holds an X-owned pointer.  A property that is absent, of the wrong type, or
    that the server refuses yields ``None``.
    """
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    item_count = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()

    status = library.XGetWindowProperty(
        display, window, prop, 0, _MAX_PROPERTY_LONGS, False, req_type,
        ctypes.byref(actual_type), ctypes.byref(actual_format),
        ctypes.byref(item_count), ctypes.byref(bytes_after), ctypes.byref(data),
    )
    if status != 0 or not data:
        return None

    try:
        if actual_type.value == 0 or item_count.value == 0:
            return None

        width = actual_format.value // 8
        # A 32-bit X property is handed back as an array of C longs, which are
        # 64 bits wide on a 64-bit host - the server's format field does not say so.
        if actual_format.value == 32:
            width = ctypes.sizeof(ctypes.c_long)

        raw = bytes(data[:item_count.value * width])
        return raw, actual_format.value, item_count.value
    finally:
        library.XFree(data)


def _read_longs(library: ctypes.CDLL, display: int, window: int, prop: int, req_type: int) -> list[int]:
    """Read a 32-bit X property as a list of integers."""
    result = _read_property(library, display, window, prop, req_type)
    if result is None:
        return []

    raw, item_format, count = result
    if item_format != 32:
        return []

    values = (ctypes.c_ulong * count).from_buffer_copy(raw)
    return [int(value) for value in values]


def _window_pid(library: ctypes.CDLL, display: int, window: int) -> int | None:
    """Return the pid that owns *window* from ``_NET_WM_PID``, or None if unset."""
    prop = library.XInternAtom(display, b'_NET_WM_PID', True)
    if not prop:
        return None

    values = _read_longs(library, display, window, prop, _XA_CARDINAL)
    return values[0] if values else None


def _window_title(library: ctypes.CDLL, display: int, window: int) -> str:
    """Return *window*'s title from ``_NET_WM_NAME``, falling back to ``WM_NAME``."""
    utf8_string = library.XInternAtom(display, b'UTF8_STRING', True)
    net_wm_name = library.XInternAtom(display, b'_NET_WM_NAME', True)

    if net_wm_name and utf8_string:
        result = _read_property(library, display, window, net_wm_name, utf8_string)
        if result is not None:
            return result[0].split(b'\x00', 1)[0].decode('utf-8', errors='replace')

    wm_name = library.XInternAtom(display, b'WM_NAME', True)
    if wm_name:
        result = _read_property(library, display, window, wm_name, _ANY_PROPERTY_TYPE)
        if result is not None:
            return result[0].split(b'\x00', 1)[0].decode('utf-8', errors='replace')

    return ''
