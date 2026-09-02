"""
Window Background
=================

Keeps the bare application window the same colour as the page's content area,
so no mismatched surface shows through behind the area exposed while the web
view lags a resize, nor before the page paints.

``window_background_color`` reads the system's light/dark preference to pick the
colour for the window created at start-up - the Windows app theme from the
registry, the desktop portal's ``color-scheme`` on Linux; a read-only lookup of
one preference either way, no credentials.  That is only a guess:
the page follows its own stored theme preference, which Python cannot see, so a
dark UI on a light system (or vice versa) would start with the wrong colour.
The page therefore reports its real content colour over the bridge once resolved
(and on every theme switch), and ``apply_native_background`` recolours the live
window to match - the form and web-view backgrounds on Windows plus the window
class brush, the window, container and web-view CSS backgrounds on Linux.

Side effects are limited to reading the theme preference and recolouring the
application's own window - no file, registry, or network writes.  The
implementations live in the platform layer; this module is the name the
application knows them by.
"""
from __future__ import annotations

from .platforms import apply_native_background, paint_window, window_background_color

__all__ = ['apply_native_background', 'paint_window', 'window_background_color']
