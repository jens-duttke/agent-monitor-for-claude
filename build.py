"""
Build Script
============

Builds a standalone EXE for Agent Monitor for Claude using PyInstaller.

Windows only - PyInstaller cannot reliably bundle GTK and WebKitGTK, so on
Linux the app is run from source instead (see the README).

Usage:
    python build.py

Produces:
    dist/AgentMonitorForClaude.exe
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / 'dist'
SPEC = ROOT / 'agent_monitor_for_claude.spec'


def build() -> None:
    """Run PyInstaller to produce the standalone EXE."""
    if sys.platform != 'win32':
        print('The standalone executable is a Windows build; on Linux, run the app from source instead:')
        print('    python -m agent_monitor_for_claude')
        sys.exit(1)

    print('Starting PyInstaller build ...')
    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', str(SPEC)]
    subprocess.check_call(cmd, cwd=str(ROOT))

    exe = DIST / 'AgentMonitorForClaude.exe'
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f'\nBuild successful!  {exe}  ({size_mb:.1f} MB)')
    else:
        print('\nBuild failed - EXE not found.')
        sys.exit(1)


if __name__ == '__main__':
    build()
