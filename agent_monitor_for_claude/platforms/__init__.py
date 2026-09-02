"""
Platform Layer
===============

Selects the operating-system backend and re-exports its API under one name, so
the rest of the package never branches on the running system.  This dispatch,
and the two alongside it (:mod:`agent_monitor_for_claude.process_probe` for
process introspection, :mod:`~agent_monitor_for_claude.single_instance` for the
instance guard), are the only places a platform check belongs.

Every backend module must import cleanly without its toolkit present: the test
suite and headless tooling import this package on either system, so the Linux
backend defers its ``gi`` imports until a function actually needs them.

A capability one system genuinely lacks says so rather than failing quietly -
:func:`paint_window` returns ``False`` on Linux because a GTK window has no
per-class background brush to point anywhere, and the caller already sets the
background that does exist.
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == 'win32'

if IS_WINDOWS:
    from .win32 import (
        DIAGNOSTIC_PACKAGES, activate_window, apply_native_background, ask_yes_no, copy_text, diagnostic_display_rows,
        diagnostic_post_init_rows, diagnostic_runtime_rows, diagnostic_system_rows, enum_windows,
        no_window_kwargs, open_path, open_uri, paint_window, prepare_gui_environment, reveal_file,
        setup_console, show_error_box, storage_dir, window_background_color,
    )
else:
    from .linux import (
        DIAGNOSTIC_PACKAGES, activate_window, apply_native_background, ask_yes_no, copy_text, diagnostic_display_rows,
        diagnostic_post_init_rows, diagnostic_runtime_rows, diagnostic_system_rows, enum_windows,
        no_window_kwargs, open_path, open_uri, paint_window, prepare_gui_environment, reveal_file,
        setup_console, show_error_box, storage_dir, window_background_color,
    )

__all__ = [
    'DIAGNOSTIC_PACKAGES', 'IS_WINDOWS', 'activate_window', 'apply_native_background', 'ask_yes_no', 'copy_text',
    'diagnostic_display_rows', 'diagnostic_post_init_rows', 'diagnostic_runtime_rows', 'diagnostic_system_rows',
    'enum_windows', 'no_window_kwargs', 'open_path', 'open_uri', 'paint_window', 'prepare_gui_environment',
    'reveal_file', 'setup_console', 'show_error_box', 'storage_dir', 'window_background_color',
]
