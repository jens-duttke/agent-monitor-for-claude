"""
Verbose Diagnostics
====================

Collects and prints system and runtime diagnostics when the app is launched
with ``--verbose``.  Helps users diagnose startup failures (a missing web-view
runtime, DPI or display issues, dependency versions) without needing a Python
installation.

The frame - the sections, the layout, the home-directory redaction - is shared;
the rows that only one system can answer come from the platform layer, so a
Linux run reports its session type and GTK/WebKitGTK versions where a Windows
run reports DPI awareness and WebView2.
"""
from __future__ import annotations

import importlib.metadata
import locale
import os
import sys
from pathlib import Path

from .platforms import (
    DIAGNOSTIC_PACKAGES, diagnostic_display_rows, diagnostic_post_init_rows, diagnostic_runtime_rows,
    diagnostic_system_rows,
)
from .platforms import setup_console as _platform_setup_console

__all__ = ['setup_console', 'print_startup_diagnostics', 'print_runtime_diagnostics']


def setup_console() -> None:
    """Ensure diagnostics have somewhere to print, and turn on pywebview's own logging.

    Attaching a console is the platform's job (only Windows needs one, and only
    for a windowed build); raising pywebview's log level is not, so it happens
    here for both systems.
    """
    _platform_setup_console()

    os.environ['PYWEBVIEW_LOG'] = 'DEBUG'


def print_startup_diagnostics() -> None:
    """Print system and environment diagnostics before the webview starts."""
    from . import __version__

    print(f'\n  Agent Monitor for Claude v{__version__} - Verbose Mode')
    print(f'  {"=" * 48}')

    _section('System')
    _rows(diagnostic_system_rows())

    _section('Python')
    _row('Version', sys.version.split()[0])
    _row('Executable', _redact_home(sys.executable))
    frozen = getattr(sys, 'frozen', False)
    _row('Frozen (PyInstaller)', str(frozen))
    if frozen:
        _row('Bundle dir', _redact_home(getattr(sys, '_MEIPASS', 'unknown')))

    _section('Locale')
    sys_locale = locale.getlocale()
    _row('System locale', f'{sys_locale[0]}, {sys_locale[1]}' if sys_locale[0] else 'not set')
    _row('CLAUDE_CONFIG_DIR', _redact_home(os.environ.get('CLAUDE_CONFIG_DIR', '')) or '(not set)')

    _section('Display')
    _rows(diagnostic_display_rows())

    _section('Runtimes')
    _rows(diagnostic_runtime_rows())

    _section('Dependencies')
    for package in DIAGNOSTIC_PACKAGES:
        _row(package, _package_version(package))

    print()


def print_runtime_diagnostics() -> None:
    """Print diagnostics only available after the webview host has loaded."""
    import webview  # type: ignore[import-untyped]  # no type stubs available

    _section('Runtime (post-init)')

    renderer = getattr(webview, 'renderer', None) or 'unknown'
    _row('Webview renderer', renderer)

    guilib = getattr(webview, 'guilib', None)
    _row('GUI backend', guilib.__name__ if guilib else 'unknown')

    _rows(diagnostic_post_init_rows())

    print()


def _section(title: str) -> None:
    """Print a section header."""
    print(f'\n  {title}')
    print(f'  {"-" * len(title)}')


def _row(label: str, value: str, indent: int = 4) -> None:
    """Print a key-value row with aligned columns."""
    print(f'{" " * indent}{label + ":":<22s} {value}')


def _rows(rows: list[tuple[str, str]]) -> None:
    """Print a platform-supplied list of key-value rows."""
    for label, value in rows:
        _row(label, value)


def _package_version(name: str) -> str:
    """Get an installed package version, or 'not found'."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return 'not found'


def _redact_home(path_str: str) -> str:
    """Replace the user's home directory with ``~`` to avoid exposing the username.

    Compares on the case- and separator-normalized paths: NTFS is
    case-insensitive and a hand-typed ``CLAUDE_CONFIG_DIR`` can differ in casing
    or slashes from ``Path.home()``, while a Linux path is compared as typed
    (``normcase`` leaves it alone there).  A plain prefix check would also
    over-match a sibling (``.../jens2`` against home ``.../jens``).
    """
    home_n = os.path.normcase(os.path.normpath(str(Path.home())))
    norm = os.path.normcase(os.path.normpath(path_str))

    if norm == home_n:
        return '~'
    if norm.startswith(home_n + os.sep):
        return '~' + path_str[len(home_n):]
    return path_str
