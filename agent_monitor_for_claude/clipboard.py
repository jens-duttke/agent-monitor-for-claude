"""
Clipboard
=========

Copies text to the Windows clipboard via Win32.  A write surface used only on
an explicit user action (the "copy session ID" menu item), never automatically.
"""
from __future__ import annotations

import ctypes

__all__ = ['copy_text']

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32

_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def copy_text(text: str) -> bool:
    """Place *text* on the clipboard as Unicode; return True on success."""
    if not isinstance(text, str) or not text:
        return False

    if not _user32.OpenClipboard(None):
        return False

    try:
        _user32.EmptyClipboard()
        data = text.encode('utf-16-le') + b'\x00\x00'
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
