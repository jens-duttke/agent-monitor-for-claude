"""
Windows Backend
================

Windows implementations of the platform API; see
:mod:`agent_monitor_for_claude.platforms` for the dispatch and for what each
function promises.  Everything here is a Win32/WinForms call against the
running desktop or the application's own window - window enumeration and
activation, the clipboard, the shell launch surfaces, the window background,
and the startup diagnostics.

Two of these are the sanctioned launch surfaces the repo's privacy statement
lists: :func:`open_path` and :func:`open_uri` hand a validated target to
``os.startfile``, and :func:`reveal_file` hands a validated file to the shell's
``SHOpenFolderAndSelectItems``, which raises an Explorer window with the item
selected and launches nothing.  Callers do the validating; this module performs
the call.  Beyond the window background, nothing here writes.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import platform
import sys
import uuid
import winreg
from pathlib import Path
from typing import Any

__all__ = [
    'DIAGNOSTIC_PACKAGES', 'activate_window', 'ask_yes_no', 'apply_native_background', 'copy_text', 'diagnostic_display_rows',
    'diagnostic_post_init_rows', 'diagnostic_runtime_rows', 'diagnostic_system_rows', 'enum_windows',
    'no_window_kwargs', 'open_path', 'open_uri', 'paint_window', 'prepare_gui_environment', 'reveal_file',
    'set_keep_on_top', 'set_min_window_size', 'set_taskbar_attention', 'setup_console', 'show_error_box', 'storage_dir',
    'window_background_color',
]

# Third-party packages worth reporting in the diagnostics output.  pythonnet and
# clr-loader are part of the WebView2 host and exist only here.
DIAGNOSTIC_PACKAGES = ('pywebview', 'pythonnet', 'clr-loader', 'psutil')

# Initial window colour, held identical to the content area's --bg in
# index.css so the bare window matches the content instead of flashing a
# mismatched colour.
_WINDOW_BACKGROUND = '#262624'

# SetClassLongPtr index for the class background brush (GCLP_HBRBACKGROUND).
_GCLP_HBRBACKGROUND = -10

# WebView2 registry GUIDs (runtime, beta, dev, canary)
_WEBVIEW2_GUIDS = [
    ('{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'Runtime'),
    ('{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}', 'Beta'),
    ('{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}', 'Developer'),
    ('{65C35B14-6C1D-4122-AC46-7148CC9D6497}', 'Canary'),
]

# MessageBoxW flags and the "Yes" result code.
_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_MB_ICONWARNING = 0x30
_MB_TOPMOST = 0x40000
_IDYES = 6

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002

# COM has to be live on the calling thread for the shell's select-in-folder
# call; every js_api call arrives on its own worker thread, where it is not.
# CoInitializeEx reports S_OK for a fresh apartment and S_FALSE when this
# thread already had one - only those two are undone again afterwards.
_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1

_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_shell32 = ctypes.windll.shell32
_ole32 = ctypes.windll.ole32

_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

# A shell id list is a pointer and must be declared as one: read back as the
# default C int, it would be truncated to 32 bits on a 64-bit build.
_shell32.ILCreateFromPathW.argtypes = [ctypes.wintypes.LPCWSTR]
_shell32.ILCreateFromPathW.restype = ctypes.c_void_p
_shell32.ILFree.argtypes = [ctypes.c_void_p]
_shell32.ILFree.restype = None
_shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.wintypes.DWORD]
_shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
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

# The taskbar button's overlay icon goes through the shell's ITaskbarList3: the
# class and interface ids, and the slots of the three methods called in its
# vtable (IUnknown takes 0-2, ITaskbarList 3-7, ITaskbarList2 8, ITaskbarList3
# from 9 on).
_CLSID_TASKBAR_LIST = '56FDF344-FD6D-11D0-958A-006097C9A090'
_IID_TASKBAR_LIST3 = 'EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF'
_CLSCTX_INPROC_SERVER = 0x1
_VTBL_RELEASE = 2
_VTBL_HR_INIT = 3
_VTBL_SET_OVERLAY_ICON = 18

# FlashWindowEx: flash the taskbar button only, twice; Windows then keeps it
# highlighted until the window is activated.
_FLASHW_TRAY = 0x2
_FLASH_COUNT = 2

# The badge: the UI's "needs you" orange, white digits, drawn at twice the
# 16 px overlay size so it stays sharp on a scaled display.
_BADGE_ARGB = (255, 0xD2, 0x4A, 0x0E)
_BADGE_SIZE = 32


class _Guid(ctypes.Structure):
    _fields_ = [
        ('data1', ctypes.c_ulong), ('data2', ctypes.c_ushort), ('data3', ctypes.c_ushort), ('data4', ctypes.c_ubyte * 8),
    ]


class _FlashWindowInfo(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.wintypes.UINT), ('hwnd', ctypes.wintypes.HWND), ('dwFlags', ctypes.wintypes.DWORD),
        ('uCount', ctypes.wintypes.UINT), ('dwTimeout', ctypes.wintypes.DWORD),
    ]


_ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_Guid), ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p),
]
_ole32.CoCreateInstance.restype = ctypes.c_long
_user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FlashWindowInfo)]
_user32.FlashWindowEx.restype = ctypes.wintypes.BOOL
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.DestroyIcon.argtypes = [ctypes.c_void_p]
_user32.DestroyIcon.restype = ctypes.wintypes.BOOL


def show_error_box(message: str, title: str) -> None:
    """Show a modal error dialog."""
    _user32.MessageBoxW(0, message, title, _MB_ICONWARNING)


def ask_yes_no(message: str, title: str) -> bool:
    """Ask a yes/no question in a modal, always-on-top dialog; True on "Yes"."""
    return _user32.MessageBoxW(None, message, title, _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST) == _IDYES


def no_window_kwargs() -> dict[str, Any]:
    """Return ``subprocess`` keyword arguments that suppress a console window.

    ``CREATE_NO_WINDOW``, so no console flashes even though this app has none
    of its own.
    """
    return {'creationflags': 0x08000000}


def prepare_gui_environment() -> None:
    """Prepare this process's environment for the GUI toolkit.

    Nothing is needed on Windows: WebView2 is a system component, and no
    launcher here rewrites the environment out from under a program the way a
    Snap-confined one does on Linux.  The hook exists on both systems because
    the entry point calls it before anything touches the toolkit; like its Linux
    counterpart it is idempotent.
    """


def storage_dir() -> Path:
    """Return the WebView2 profile directory used for UI preference storage."""
    base = os.environ.get('LOCALAPPDATA')
    root = Path(base) if base else Path.home() / 'AppData' / 'Local'
    return root / 'AgentMonitorForClaude'


def user_config_dir() -> Path:
    """Return the directory the app's own settings file is written to (the roaming application data folder)."""
    base = os.environ.get('APPDATA')
    root = Path(base) if base else Path.home() / 'AppData' / 'Roaming'
    return root / 'AgentMonitorForClaude'


def copy_text(text: str) -> bool:
    """Place *text* on the clipboard as Unicode; return True on success."""
    # Encode before touching the clipboard: a lone UTF-16 surrogate (which can
    # survive json.loads over the bridge) raises here, and doing it first keeps
    # the operation atomic - a failed copy must not have already emptied the
    # existing clipboard contents.
    try:
        data = text.encode('utf-16-le') + b'\x00\x00'
    except UnicodeEncodeError:
        return False

    if not _user32.OpenClipboard(None):
        return False

    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not handle:
            return False

        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            _kernel32.GlobalFree(handle)
            return False

        ctypes.memmove(pointer, data, len(data))
        _kernel32.GlobalUnlock(handle)

        # On success the system takes ownership of the handle; on failure it does
        # not, so the buffer must be freed here rather than leaked.
        if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            return False

        return True
    finally:
        _user32.CloseClipboard()


def open_path(path: str) -> bool:
    """Open an already-validated directory in Windows Explorer.

    For a folder, ``os.startfile`` is routed to Explorer by the shell.
    """
    try:
        os.startfile(path)
    except OSError:
        return False

    return True


def open_uri(uri: str) -> bool:
    """Hand an already-validated URI to its registered handler."""
    try:
        os.startfile(uri)
    except OSError:
        return False

    return True


def reveal_file(path: str) -> bool:
    """Raise an Explorer window on *path*'s folder with *path* selected.

    Wraps the shell's ``SHOpenFolderAndSelectItems``, which takes shell id lists
    rather than path strings: one for the folder, one for the item inside it.
    The file is only ever *shown*, never opened, so no program is launched for it
    and its content is never handed to another application.  COM is initialized
    for the calling thread and released again only when this call is what
    initialized it.  Every failure along the way - an id list the namespace
    cannot build, a refusing shell - is reported as False, leaving the caller to
    fall back on the plain folder.
    """
    folder = None
    item = None
    com_ready = False

    try:
        com_ready = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED) in (_S_OK, _S_FALSE)

        folder = _shell32.ILCreateFromPathW(os.path.dirname(path))
        item = _shell32.ILCreateFromPathW(path)
        if not folder or not item:
            return False

        items = (ctypes.c_void_p * 1)(item)

        # SUCCEEDED(hr): any non-negative HRESULT means the window was raised.
        return _shell32.SHOpenFolderAndSelectItems(folder, 1, items, 0) >= 0
    except OSError:
        return False
    finally:
        if folder:
            _shell32.ILFree(folder)
        if item:
            _shell32.ILFree(item)
        if com_ready:
            _ole32.CoUninitialize()


def enum_windows() -> list[tuple[int, int, str]]:
    """Return all visible, titled top-level windows as ``(hwnd, pid, title)``."""
    windows: list[tuple[int, int, str]] = []

    def _collect(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True

        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)

        window_pid = ctypes.wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))

        windows.append((hwnd, window_pid.value, buffer.value))
        return True

    _user32.EnumWindows(_EnumWindowsProc(_collect), 0)
    return windows


def activate_window(handle: int) -> bool:
    """Restore and raise a window to the foreground."""
    if _user32.IsIconic(handle):
        _user32.ShowWindow(handle, _SW_RESTORE)

    if _user32.SetForegroundWindow(handle):
        return True

    # Windows refuses foreground changes in some states; a synthetic ALT tap
    # is the documented workaround to lift that restriction.
    _user32.keybd_event(_VK_MENU, 0, 0, 0)
    result = _user32.SetForegroundWindow(handle)
    _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)

    return bool(result)


def window_background_color() -> str:
    """Return the content-area background colour the window starts with (the UI has one, dark theme)."""
    return _WINDOW_BACKGROUND


def paint_window(handle: int, hex_color: str) -> bool:
    """Point the class background brush of *handle* and its children at *hex_color*.

    The class brush is what Windows uses to fill newly exposed client area
    during a resize (and before the first paint), so pointing it at the content
    colour removes the white flash the default brush would show.

    Parameters
    ----------
    handle : int
        Handle of the application's top-level window.
    hex_color : str
        Target colour as ``#rrggbb``.

    Returns
    -------
    bool
        True if a valid handle and colour were applied.
    """
    color = _colorref(hex_color)
    if not handle or color is None:
        return False

    _paint_class(handle, color)

    def _paint_child(child_handle: int, _lparam: int) -> bool:
        _paint_class(child_handle, color)
        return True

    callback = _EnumChildProc(_paint_child)
    _user32.EnumChildWindows(handle, callback, 0)
    _user32.InvalidateRect(handle, None, True)
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


def set_min_window_size(native: object, width: int, height: int) -> bool:
    """Change the smallest size a live WinForms window can be resized to.

    pywebview fixes the minimum once, at creation; the compact view needs a
    narrower one only while it is on.  The form's ``MinimumSize`` is in physical
    pixels, so the logical size is scaled by the window's own DPI factor, and the
    assignment is marshalled onto the UI thread like every other form property.

    Parameters
    ----------
    native : object
        pywebview's native window (the WinForms ``BrowserForm``).
    width, height : int
        New minimum size in logical pixels.

    Returns
    -------
    bool
        True if the minimum was applied.  Best-effort otherwise: the .NET
        interop only exists inside a running WebView2 host, so any failure
        (including off a real window) is swallowed and reported as False.
    """
    if native is None or width < 1 or height < 1:
        return False

    try:
        # Imported lazily for the same reason as in apply_native_background.
        import clr

        clr.AddReference('System.Drawing')
        from System import Action
        from System.Drawing import Size

        scale = float(getattr(native, '_scale', 1.0) or 1.0)

        def _assign() -> None:
            native.MinimumSize = Size(int(width * scale), int(height * scale))

        if native.InvokeRequired:
            native.Invoke(Action(_assign))
        else:
            _assign()

        return True
    except Exception:  # noqa: BLE001  # .NET interop only present at runtime
        return False


def set_keep_on_top(native: object, on_top: bool) -> bool:
    """Keep a live WinForms window above all others, or release it.

    Never assigned from the calling thread, which is what pywebview's own
    ``on_top`` does: ``TopMost`` is a property, and pythonnet keeps the GIL
    across a property assignment, while the assignment sends a message to the
    UI thread and waits for it.  A UI thread that is meanwhile running Python -
    another bridge call's ``Invoke``, such as the window background's - waits
    for the GIL in turn, and the whole application hangs.  ``Invoke`` is a
    method call, across which pythonnet releases the GIL.

    Parameters
    ----------
    native : object
        pywebview's native window (the WinForms ``BrowserForm``).
    on_top : bool
        Whether the window stays above all others.

    Returns
    -------
    bool
        True if the flag was applied.  Best-effort otherwise: the .NET interop
        only exists inside a running WebView2 host, so any failure (including
        off a real window) is swallowed and reported as False.
    """
    if native is None:
        return False

    try:
        # Imported lazily for the same reason as in apply_native_background.
        from System import Action

        def _assign() -> None:
            native.TopMost = on_top

        if native.InvokeRequired:
            native.Invoke(Action(_assign))
        else:
            _assign()

        return True
    except Exception:  # noqa: BLE001  # .NET interop only present at runtime
        return False


def set_taskbar_attention(native: object, badge: str, flash: bool) -> bool:
    """Show a badge on the window's taskbar button, and flash the button on request.

    The badge is an overlay icon drawn from *badge* (``''`` removes it); the
    flash runs only while the window is not in front, twice, after which Windows
    keeps the button highlighted until the window is activated.  Both are calls
    on the application's own taskbar button - no file, no registry, no other
    window.  Marshalled onto the UI thread like every other window call: the
    taskbar object belongs to the UI thread's COM apartment.

    Parameters
    ----------
    native : object
        pywebview's native window (the WinForms ``BrowserForm``).
    badge : str
        The text to show on the button, at most a few characters.
    flash : bool
        Whether to flash the button.

    Returns
    -------
    bool
        True if the badge was applied.  Best-effort otherwise: the .NET interop
        only exists inside a running WebView2 host, so any failure (including
        off a real window) is swallowed and reported as False.
    """
    if native is None or not isinstance(badge, str):
        return False

    try:
        from System import Action

        applied = False

        def _apply() -> None:
            nonlocal applied
            try:
                hwnd = int(native.Handle.ToInt64())
                applied = _set_overlay_badge(hwnd, badge)
                if flash and _user32.GetForegroundWindow() != hwnd:
                    info = _FlashWindowInfo(ctypes.sizeof(_FlashWindowInfo), hwnd, _FLASHW_TRAY, _FLASH_COUNT, 0)
                    _user32.FlashWindowEx(ctypes.byref(info))
            except Exception:  # noqa: BLE001  # a shell failure must not raise into the UI thread
                applied = False

        if native.InvokeRequired:
            native.Invoke(Action(_apply))
        else:
            _apply()

        return applied
    except Exception:  # noqa: BLE001  # .NET interop only present at runtime
        return False


def _set_overlay_badge(hwnd: int, badge: str) -> bool:
    """Set (or with an empty *badge* remove) the taskbar button's overlay icon."""
    taskbar = ctypes.c_void_p()
    hresult = _ole32.CoCreateInstance(
        ctypes.byref(_guid(_CLSID_TASKBAR_LIST)), None, _CLSCTX_INPROC_SERVER, ctypes.byref(_guid(_IID_TASKBAR_LIST3)),
        ctypes.byref(taskbar),
    )
    if hresult != _S_OK or not taskbar:
        return False

    icon = _badge_icon(badge) if badge else None
    try:
        if _com_method(taskbar, _VTBL_HR_INIT)(taskbar) != _S_OK:
            return False
        set_overlay = _com_method(taskbar, _VTBL_SET_OVERLAY_ICON, ctypes.wintypes.HWND, ctypes.c_void_p, ctypes.wintypes.LPCWSTR)
        return set_overlay(taskbar, hwnd, icon, badge or None) == _S_OK
    finally:
        # The taskbar keeps its own copy of the icon.
        if icon:
            _user32.DestroyIcon(icon)
        _com_method(taskbar, _VTBL_RELEASE, restype=ctypes.c_ulong)(taskbar)


def _com_method(pointer: ctypes.c_void_p, index: int, *argtypes: Any, restype: Any = ctypes.c_long) -> Any:
    """The method in slot *index* of a COM object's vtable, called with the object first."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _guid(text: str) -> _Guid:
    """A GUID structure from its ``XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX`` form."""
    value = uuid.UUID(text)
    data4 = (ctypes.c_ubyte * 8)(*value.bytes[8:])
    return _Guid(value.time_low, value.time_mid, value.time_hi_version, data4)


def _badge_icon(text: str) -> int | None:
    """Draw *text* white on an orange disc and return the icon handle (the caller destroys it)."""
    import clr

    clr.AddReference('System.Drawing')
    from System.Drawing import (
        Bitmap, Brushes, Color, Font, FontStyle, Graphics, GraphicsUnit, RectangleF, SolidBrush, StringAlignment, StringFormat,
    )
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Drawing.Text import TextRenderingHint

    bitmap = Bitmap(_BADGE_SIZE, _BADGE_SIZE)
    graphics = Graphics.FromImage(bitmap)
    disc = SolidBrush(Color.FromArgb(*_BADGE_ARGB))
    font = Font('Segoe UI', 20.0 if len(text) == 1 else 15.0, FontStyle.Bold, GraphicsUnit.Pixel)
    layout = StringFormat()
    try:
        graphics.SmoothingMode = SmoothingMode.AntiAlias
        graphics.TextRenderingHint = TextRenderingHint.AntiAliasGridFit
        graphics.Clear(Color.Transparent)
        graphics.FillEllipse(disc, 0, 0, _BADGE_SIZE - 1, _BADGE_SIZE - 1)
        layout.Alignment = StringAlignment.Center
        layout.LineAlignment = StringAlignment.Center
        graphics.DrawString(text, font, Brushes.White, RectangleF(0.0, 1.0, float(_BADGE_SIZE), float(_BADGE_SIZE)), layout)
    finally:
        # GDI+ handles are not freed until the garbage collector finalizes them.
        for resource in (layout, font, disc, graphics):
            resource.Dispose()

    try:
        return int(bitmap.GetHicon().ToInt64()) or None
    finally:
        bitmap.Dispose()


def setup_console() -> None:
    """Attach a console so diagnostics have somewhere to print.

    A stream the shell already connected - a console session, or output the user
    redirected to a file (``--verbose > diag.txt``) - is left untouched;
    overwriting it with the console buffer (``CONOUT$``) would send everything to
    the console and produce an empty redirect target.  Only a missing/detached
    stream (a frozen windowed build, where ``sys.stdout`` is ``None``) gets a
    console attached and bound.
    """
    ATTACH_PARENT_PROCESS = -1

    have_out = _stream_usable(sys.stdout)
    have_err = _stream_usable(sys.stderr)

    if have_out and have_err:
        return

    if not _kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        _kernel32.AllocConsole()

    # errors='backslashreplace' (Python's own stderr default) so a lone
    # surrogate in any diagnostic degrades to an escape instead of raising.
    if not have_out:
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='backslashreplace')  # noqa: SIM115
    if not have_err:
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='backslashreplace')  # noqa: SIM115


def diagnostic_system_rows() -> list[tuple[str, str]]:
    """Return the ``System`` diagnostics rows."""
    winver = sys.getwindowsversion()
    return [
        ('OS', f'{platform.platform()} (build {winver.build})'),
        ('Architecture', platform.machine()),
    ]


def diagnostic_display_rows() -> list[tuple[str, str]]:
    """Return the ``Display`` diagnostics rows (DPI awareness mode and system DPI)."""
    try:
        ctx = _user32.GetThreadDpiAwarenessContext()
        awareness = _user32.GetAwarenessFromDpiAwarenessContext(ctx)
        awareness_names = {0: 'Unaware', 1: 'System', 2: 'Per-Monitor V2'}
        awareness_str = awareness_names.get(awareness, f'Unknown ({awareness})')
    except Exception:  # noqa: BLE001  # any Win32 failure degrades to "unavailable"
        awareness_str = 'unavailable'

    try:
        dpi = _user32.GetDpiForSystem()
        dpi_str = f'{dpi} ({round(dpi / 96 * 100)}%)'
    except Exception:  # noqa: BLE001  # any Win32 failure degrades to "unavailable"
        dpi_str = 'unavailable'

    return [('DPI awareness', awareness_str), ('System DPI', dpi_str)]


def diagnostic_runtime_rows() -> list[tuple[str, str]]:
    """Return the ``Runtimes`` diagnostics rows (WebView2 and .NET Framework)."""
    return [('WebView2', _webview2_version()), ('.NET Framework', _dotnet_version())]


def diagnostic_post_init_rows() -> list[tuple[str, str]]:
    """Return diagnostics rows only available once the webview host has loaded."""
    return []


def _stream_usable(stream: object) -> bool:
    """Return True if *stream* is a real, connected stream (not None or detached)."""
    if stream is None:
        return False
    try:
        stream.fileno()  # type: ignore[attr-defined]  # guarded by the except below
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _webview2_version() -> str:
    """Read the WebView2 runtime version from the registry."""
    for guid, channel in _WEBVIEW2_GUIDS:
        for root_key in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub_path in (
                rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}',
                rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}',
            ):
                try:
                    with winreg.OpenKey(root_key, sub_path) as key:
                        build, _ = winreg.QueryValueEx(key, 'pv')
                        if build and build != '0.0.0.0':
                            suffix = f' ({channel})' if channel != 'Runtime' else ''
                            return f'{build}{suffix}'
                except OSError:
                    pass

    return 'not found'


def _dotnet_version() -> str:
    """Read the .NET Framework version from the registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full') as key:
            release, _ = winreg.QueryValueEx(key, 'Release')
            if not isinstance(release, int):
                # A damaged registry can hold a non-DWORD Release; comparing it
                # against the integer thresholds below would raise TypeError.
                return 'not found'
            version_map = [
                (533320, '4.8.1'), (528040, '4.8'), (461808, '4.7.2'), (461308, '4.7.1'),
                (460798, '4.7'), (394802, '4.6.2'), (394254, '4.6.1'), (393295, '4.6'),
            ]
            for min_release, version in version_map:
                if release >= min_release:
                    return f'{version} (release {release})'
            return f'< 4.6 (release {release})'
    except OSError:
        return 'not found'


def _paint_class(handle: int, color: int) -> None:
    """Set one window's class background brush to a solid *color*."""
    brush = _gdi32.CreateSolidBrush(color)
    if brush:
        _set_class_long_ptr(handle, _GCLP_HBRBACKGROUND, brush)


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
