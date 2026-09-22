"""
Build Script
============

Builds a standalone EXE for Agent Monitor for Claude using PyInstaller and code
signs it when a certificate is configured.

Windows only - PyInstaller cannot reliably bundle GTK and WebKitGTK, so on
Linux the app is run from source instead (see the README).

Signing is optional. Without a `signing.env` beside this script the build
produces the same unsigned EXE it always did, so anyone who clones the
repository can build it. That file is git-ignored and holds no secret: the
token's PIN is never stored, signtool asks the token for it on every run.

Usage:
    python build.py

Produces:
    dist/AgentMonitorForClaude.exe
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / 'dist'
SPEC = ROOT / 'agent_monitor_for_claude.spec'
SIGNING_CONFIG = ROOT / 'signing.env'


def build() -> None:
    """Run PyInstaller to produce the standalone EXE, then sign it when configured."""
    if sys.platform != 'win32':
        print('The standalone executable is a Windows build; on Linux, run the app from source instead:')
        print('    python -m agent_monitor_for_claude')
        sys.exit(1)

    print('Starting PyInstaller build ...')
    cmd = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', str(SPEC)]
    subprocess.check_call(cmd, cwd=str(ROOT))

    exe = DIST / 'AgentMonitorForClaude.exe'
    if not exe.exists():
        print('\nBuild failed - EXE not found.')
        sys.exit(1)

    _sign(exe)

    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f'\nBuild successful!  {exe}  ({size_mb:.1f} MB)')


def _sign(exe: Path) -> None:
    """
    Code sign the executable in place and verify what came out.

    A build without `signing.env` returns immediately. A configured signature
    that fails stops the build instead of leaving an unsigned EXE behind, which
    would otherwise reach a release page looking finished.

    A failed attempt is repeated against the fallback timestamp server when one
    is configured, because the cause is not knowable here: a mistyped PIN fails
    exactly like a timestamp server that did not answer, and a second try costs
    one more PIN prompt. Neither message therefore names a cause - signtool's
    own output does that.

    Parameters
    ----------
    exe : Path
        The freshly built executable.
    """
    config = _signing_config()
    if config is None:
        print('\nNo signing.env - the EXE stays unsigned.')
        return

    signtool = _signtool()
    timestamp_urls = [config['SIGNING_TIMESTAMP_URL']]
    fallback = config.get('SIGNING_TIMESTAMP_FALLBACK_URL')
    if fallback:
        timestamp_urls.append(fallback)

    print('\nSigning the EXE - the token asks for its PIN ...')
    for attempt, url in enumerate(timestamp_urls, start=1):
        if attempt > 1:
            print(f'\nTrying again through {url} - the token asks for its PIN once more ...')

        cmd = [str(signtool), 'sign', '/sha1', config['SIGNING_THUMBPRINT'], '/fd', 'SHA256', '/tr', url, '/td', 'SHA256', str(exe)]
        if subprocess.call(cmd) == 0:
            _verify(exe, signtool)
            return

        print(f'Signing through {url} failed.')

    print("Signing failed - signtool's message above says why: a wrong PIN, an unmatched thumbprint, or a timestamp server that did not answer.")
    sys.exit(1)


def _signing_config() -> dict[str, str] | None:
    """
    Read the KEY=value pairs from `signing.env`.

    Returns
    -------
    dict[str, str] | None
        The configuration, or None when the file does not exist.
    """
    if not SIGNING_CONFIG.exists():
        return None

    config: dict[str, str] = {}
    for raw in SIGNING_CONFIG.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue

        key, separator, value = line.partition('=')
        if not separator:
            print(f'{SIGNING_CONFIG.name}: line is not KEY=value: {line}')
            sys.exit(1)

        config[key.strip()] = value.strip()

    for key in ('SIGNING_THUMBPRINT', 'SIGNING_TIMESTAMP_URL'):
        if not config.get(key):
            print(f'{SIGNING_CONFIG.name}: {key} is missing or empty')
            sys.exit(1)

    return config


def _signtool() -> Path:
    """
    Locate the newest 64-bit signtool.exe among the installed Windows SDKs.

    Returns
    -------
    Path
        The signtool to sign with.
    """
    program_files = [os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')]
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for base in program_files:
        if not base:
            continue

        root = Path(base) / 'Windows Kits' / '10' / 'bin'
        if not root.is_dir():
            continue

        for entry in root.iterdir():
            tool = entry / 'x64' / 'signtool.exe'
            if tool.is_file():
                candidates.append((_sdk_version(entry.name), tool))

    if not candidates:
        print('signtool.exe not found - install the Windows SDK signing tools.')
        sys.exit(1)

    return max(candidates)[1]


def _sdk_version(name: str) -> tuple[int, ...]:
    """Sort key for an SDK directory name such as `10.0.19041.0`; anything else sorts last."""
    parts = name.split('.')
    if not all(part.isdigit() for part in parts):
        return (0,)

    return tuple(int(part) for part in parts)


def _verify(exe: Path, signtool: Path) -> None:
    """
    Prove the signed executable verifies rather than trusting the signing step.

    `/pa` applies the Authenticode policy, so the chain has to reach a root this
    machine trusts, and `/tw` makes signtool flag a missing timestamp. That a
    timestamp exists is already settled here: signing runs with `/tr`, and
    signtool fails the signing step when no timestamp server answers. Without
    one the signature would stop verifying the day the certificate expires, and
    a download published a year earlier would count as unsigned.

    Parameters
    ----------
    exe : Path
        The executable that was just signed.
    signtool : Path
        The signtool that signed it.
    """
    if subprocess.call([str(signtool), 'verify', '/pa', '/tw', str(exe)]) != 0:
        print('The signed EXE did not verify.')
        sys.exit(1)


if __name__ == '__main__':
    build()
