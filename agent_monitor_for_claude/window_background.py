"""
Window Background
=================

Keeps the bare application window the same colour as the page's content area,
so no mismatched surface shows through behind the area exposed while the web
view lags a resize, nor before the page paints.

``window_background_color`` is the colour the window is created with - the
content colour of the UI's one, dark theme.  The page still reports its real
content colour over the bridge once loaded, and ``apply_native_background``
recolours the live window to match - the form and web-view backgrounds on
Windows plus the window class brush, the window, container and web-view CSS
backgrounds on Linux - so the two can never drift apart unnoticed.

Side effects are limited to recolouring the application's own window - no file,
registry, or network writes.  The implementations live in the platform layer;
this module is the name the application knows them by.
"""
from __future__ import annotations

from .platforms import apply_native_background, paint_window, window_background_color

__all__ = ['apply_native_background', 'paint_window', 'window_background_color']
