"""
Window Layout
=============

Resizes the application's own window for the compact view: entering it narrows
the window to ``COMPACT_WIDTH`` (keeping its height) and lowers the minimum size
the user can drag it to; leaving it restores the size the window had before and
the normal minimum, which the full layout needs to fit its controls.

Side effects are limited to sizing the application's own window - no file,
registry, or network writes.  Sizes are logical pixels throughout, which is
what pywebview's ``width``/``height``/``resize`` speak; the platform layer's
``set_min_window_size`` converts where the toolkit needs physical ones.
"""
from __future__ import annotations

from typing import Any

from .platforms import set_min_window_size

__all__ = ['COMPACT_WIDTH', 'NORMAL_MIN_SIZE', 'enter_compact', 'leave_compact']

# Smallest window the full layout fits into: below this the header and the two
# toolbar dropdowns overflow.  Passed to create_window as min_size too.
NORMAL_MIN_SIZE = (480, 360)

# The compact layout shows a dot, a title and an age per row, so it fits a much
# narrower window - wide enough for a readable title beside the header buttons.
COMPACT_WIDTH = 380
_COMPACT_MIN_SIZE = (300, 240)


def enter_compact(window: Any) -> tuple[int, int] | None:
    """Narrow *window* to the compact width and return the size to restore later.

    The minimum is lowered first, since a toolkit clamps a resize to the minimum
    in force at that moment.

    Parameters
    ----------
    window
        The pywebview window.

    Returns
    -------
    tuple of int or None
        The window's logical ``(width, height)`` before the change, or ``None``
        when the window could not be measured or resized (nothing was changed
        then beyond, at most, the minimum size).
    """
    try:
        size = (int(window.width), int(window.height))
    except Exception:  # noqa: BLE001  # a window that is closing or not shown yet
        return None

    set_min_window_size(getattr(window, 'native', None), *_COMPACT_MIN_SIZE)
    try:
        window.resize(COMPACT_WIDTH, size[1])
    except Exception:  # noqa: BLE001  # best-effort, the layout switch still applies
        return None

    return size


def leave_compact(window: Any, previous: tuple[int, int] | None) -> bool:
    """Give *window* back its pre-compact size and the normal minimum.

    The resize comes first, so the raised minimum never has to push a narrow
    window out on its own.  A missing *previous* size (the window could not be
    measured on entry) keeps the current height and widens to the normal minimum.

    Parameters
    ----------
    window
        The pywebview window.
    previous
        The size :func:`enter_compact` returned.

    Returns
    -------
    bool
        True if the window was resized.
    """
    min_width, min_height = NORMAL_MIN_SIZE
    try:
        width, height = previous if previous else (min_width, int(window.height))
        window.resize(max(width, min_width), max(height, min_height))
        resized = True
    except Exception:  # noqa: BLE001  # a window that is closing
        resized = False

    set_min_window_size(getattr(window, 'native', None), min_width, min_height)

    return resized
