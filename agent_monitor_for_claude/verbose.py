"""
Verbose Diagnostics
====================

Collects and prints system and runtime diagnostics when the app is launched
with ``--verbose``.  Helps users diagnose startup failures (missing WebView2,
DPI issues, dependency versions) without needing a Python installation.
"""
from __future__ import annotations

import ctypes
import importlib.metadata
import locale
import os
import platform
import sys
import winreg
from pathlib import Path

__all__ = ['setup_console', 'print_startup_diagnostics', 'print_runtime_diagnostics']

# WebView2 registry GUIDs (runtime, beta, dev, canary)
_WEBVIEW2_GUIDS = [
    ('{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'Runtime'),
    ('{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}', 'Beta'),
    ('{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}', 'Developer'),
    ('{65C35B14-6C1D-4122-AC46-7148CC9D6497}', 'Canary'),
]


def setup_console() -> None:
    """Attach to the parent console or allocate a new one and redirect output."""
    ATTACH_PARENT_PROCESS = -1

    if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
        ctypes.windll.kernel32.AllocConsole()

    sys.stdout = open('CONOUT$', 'w', encoding='utf-8')  # noqa: SIM115
    sys.stderr = open('CONOUT$', 'w', encoding='utf-8')  # noqa: SIM115

    os.environ['PYWEBVIEW_LOG'] = 'DEBUG'


def _section(title: str) -> None:
    """Print a section header."""
    print(f'\n  {title}')
    print(f'  {"-" * len(title)}')


def _row(label: str, value: str, indent: int = 4) -> None:
    """Print a key-value row with aligned columns."""
    print(f'{" " * indent}{label + ":":<22s} {value}')


def _package_version(name: str) -> str:
    """Get an installed package version, or 'not found'."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return 'not found'


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


def _dpi_info() -> tuple[str, str]:
    """Get the DPI awareness mode and system DPI."""
    user32 = ctypes.windll.user32

    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        awareness = user32.GetAwarenessFromDpiAwarenessContext(ctx)
        awareness_names = {0: 'Unaware', 1: 'System', 2: 'Per-Monitor V2'}
        awareness_str = awareness_names.get(awareness, f'Unknown ({awareness})')
    except Exception:
        awareness_str = 'unavailable'

    try:
        dpi = user32.GetDpiForSystem()
        scale = round(dpi / 96 * 100)
        dpi_str = f'{dpi} ({scale}%)'
    except Exception:
        dpi_str = 'unavailable'

    return awareness_str, dpi_str


def _redact_home(path_str: str) -> str:
    """Replace the user's home directory with ``~`` to avoid exposing the username.

    Compares on the case- and separator-normalized paths: NTFS is
    case-insensitive and a hand-typed ``CLAUDE_CONFIG_DIR`` can differ in casing
    or slashes from ``Path.home()``, and a plain prefix check would also
    over-match a sibling (``...\\jens2`` against home ``...\\jens``).
    """
    home_n = os.path.normcase(os.path.normpath(str(Path.home())))
    norm = os.path.normcase(os.path.normpath(path_str))

    if norm == home_n:
        return '~'
    if norm.startswith(home_n + os.sep):
        return '~' + path_str[len(home_n):]
    return path_str


def print_startup_diagnostics() -> None:
    """Print system and environment diagnostics before the webview starts."""
    from . import __version__

    print(f'\n  Agent Monitor for Claude v{__version__} - Verbose Mode')
    print(f'  {"=" * 48}')

    _section('System')
    winver = sys.getwindowsversion()
    _row('OS', f'{platform.platform()} (build {winver.build})')
    _row('Architecture', platform.machine())

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
    awareness_str, dpi_str = _dpi_info()
    _row('DPI awareness', awareness_str)
    _row('System DPI', dpi_str)

    _section('Runtimes')
    _row('WebView2', _webview2_version())
    _row('.NET Framework', _dotnet_version())

    _section('Dependencies')
    for pkg in ('pywebview', 'pythonnet', 'clr-loader', 'psutil'):
        _row(pkg, _package_version(pkg))

    print()


def print_runtime_diagnostics() -> None:
    """Print diagnostics only available after the webview/CLR has loaded."""
    import webview  # type: ignore[import-untyped]  # no type stubs available

    _section('Runtime (post-init)')

    renderer = getattr(webview, 'renderer', None) or 'unknown'
    _row('Webview renderer', renderer)

    guilib = getattr(webview, 'guilib', None)
    _row('GUI backend', guilib.__name__ if guilib else 'unknown')

    print()
