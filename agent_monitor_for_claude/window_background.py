"""
Window Background
=================

Keeps the bare application window the same colour as the page's content area,
so no mismatched surface shows through behind the area exposed while WebView2
lags a resize, nor before the page paints.

``window_background_color`` reads the Windows app theme (a read-only registry
lookup, no credentials) to pick the dark or light content colour for the window
created at start-up.  That is only a guess: the page follows its own stored
theme preference, which Python cannot see, so a dark UI on a light system (or
vice versa) would start with the wrong colour.  The page therefore reports its
real content colour over the bridge once resolved (and on every theme switch),
and ``apply_native_background`` recolours the live window to match:

* the WinForms form ``BackColor`` - what paints the client area exposed while
  WebView2 has not yet caught up with a resize;
* the WebView2 ``DefaultBackgroundColor`` - its own clear colour;
* the window-class background brush (``paint_window``) - what Windows fills
  freshly uncovered area with during a resize, before either repaints.

Side effects are limited to reading the theme preference and recolouring the
application's own window - no file, registry, or network writes.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import winreg

__all__ = ['apply_native_background', 'paint_window', 'window_background_color']

# Initial window colours, held identical to the content area's --bg in
# index.css so the bare window matches the content instead of flashing a
# mismatched colour.  The page itself picks the stored theme (or the system
# preference) before first paint.
_WINDOW_BACKGROUND_DARK = '#191918'
_WINDOW_BACKGROUND_LIGHT = '#faf9f5'

_THEME_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize'

# SetClassLongPtr index for the class background brush (GCLP_HBRBACKGROUND).
_GCLP_HBRBACKGROUND = -10

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

_EnumChildProc = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

# SetClassLongPtrW is the 64-bit entry point; the plain SetClassLongW name is
# the 32-bit fallback.  Explicit argtypes keep the brush handle from being
# truncated to 32 bits on a 64-bit process.
try:
    _set_class_long_ptr = _user32.SetClassLongPtrW
except AttributeError:  # pragma: no cover - 32-bit hosts only
    _set_class_long_ptr = _user32.SetClassLongW

_set_class_long_ptr.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_set_class_long_ptr.restype = ctypes.c_void_p

_gdi32.CreateSolidBrush.argtypes = [ctypes.wintypes.COLORREF]
_gdi32.CreateSolidBrush.restype = ctypes.c_void_p


def window_background_color() -> str:
    """Return the content-area background colour matching the Windows app theme.

    Returns
    -------
    str
        The light content colour when the app theme is light, otherwise the
        dark one; the dark colour also stands in when the preference is missing.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY) as key:
            apps_use_light, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
    except OSError:
        return _WINDOW_BACKGROUND_DARK

    return _WINDOW_BACKGROUND_LIGHT if apps_use_light else _WINDOW_BACKGROUND_DARK


def paint_window(hwnd: int, hex_color: str) -> bool:
    """Point the class background brush of *hwnd* and its children at *hex_color*.

    The class brush is what Windows uses to fill newly exposed client area
    during a resize (and before the first paint), so pointing it at the content
    colour removes the white flash the default brush would show.

    Parameters
    ----------
    hwnd : int
        Handle of the application's top-level window.
    hex_color : str
        Target colour as ``#rrggbb``.

    Returns
    -------
    bool
        True if a valid handle and colour were applied.
    """
    color = _colorref(hex_color)
    if not hwnd or color is None:
        return False

    _paint_class(hwnd, color)

    def _paint_child(child_hwnd: int, _lparam: int) -> bool:
        _paint_class(child_hwnd, color)
        return True

    callback = _EnumChildProc(_paint_child)
    _user32.EnumChildWindows(hwnd, callback, 0)
    _user32.InvalidateRect(hwnd, None, True)
    return True


def apply_native_background(native: object, hex_color: str) -> bool:
    """Recolour a live WinForms/WebView2 window to *hex_color*.

    Sets the form ``BackColor`` and the WebView2 ``DefaultBackgroundColor``, and
    also repaints the class brush via ``paint_window``.  The assignment is
    marshalled onto the UI thread, since the bridge call arrives on a worker
    thread and WinForms properties are single-threaded.

    Parameters
    ----------
    native : object
        pywebview's native window (the WinForms ``BrowserForm``).
    hex_color : str
        Target colour as ``#rrggbb``.

    Returns
    -------
    bool
        True if the colour was valid and applied.  Best-effort otherwise: the
        .NET interop only exists inside a running WebView2 host, so any failure
        (including off a real window) is swallowed and reported as False.
    """
    if native is None or _colorref(hex_color) is None:
        return False

    try:
        # Imported lazily: the .NET runtime is only present with a live host,
        # so this stays out of the module's import-time and test surface.
        import clr

        clr.AddReference('System.Drawing')
        from System import Action
        from System.Drawing import ColorTranslator

        color = ColorTranslator.FromHtml(hex_color)

        def _assign() -> None:
            native.BackColor = color
            browser = getattr(native, 'browser', None)
            webview = getattr(browser, 'webview', None)
            if webview is not None:
                webview.DefaultBackgroundColor = color

            handle = getattr(native, 'Handle', None)
            if handle is not None:
                paint_window(int(handle.ToInt64()), hex_color)

        if native.InvokeRequired:
            native.Invoke(Action(_assign))
        else:
            _assign()

        return True
    except Exception:  # noqa: BLE001  # .NET interop only present at runtime
        return False


def _paint_class(hwnd: int, color: int) -> None:
    """Set one window's class background brush to a solid *color*."""
    brush = _gdi32.CreateSolidBrush(color)
    if brush:
        _set_class_long_ptr(hwnd, _GCLP_HBRBACKGROUND, brush)


def _colorref(hex_color: str) -> int | None:
    """Convert ``#rrggbb`` to a Win32 ``0x00bbggrr`` COLORREF, or None if malformed."""
    value = (hex_color or '').lstrip('#')
    if len(value) != 6:
        return None

    try:
        red = int(value[0:2], 16)
        green = int(value[2:4], 16)
        blue = int(value[4:6], 16)
    except ValueError:
        return None

    return red | (green << 8) | (blue << 16)
