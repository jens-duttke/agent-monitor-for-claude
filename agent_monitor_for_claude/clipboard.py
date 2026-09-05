"""
Clipboard
=========

Copies text to the system clipboard.  A write surface used only on an explicit
user action (the "copy session ID" menu item), never automatically.  The
platform layer performs the write - Win32 on Windows, GTK on Linux; this module
holds the one guard both share.
"""
from __future__ import annotations

from .platforms import copy_text as _platform_copy_text

__all__ = ['copy_text']


def copy_text(text: str) -> bool:
    """Place *text* on the clipboard; return True on success.

    An empty or non-string value is refused outright rather than handed to the
    platform: it can only come from a malformed bridge call, and emptying the
    user's clipboard is not what a failed copy should do.
    """
    if not isinstance(text, str) or not text:
        return False

    return _platform_copy_text(text)
