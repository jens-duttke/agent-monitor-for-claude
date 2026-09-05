# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Agent Monitor for Claude.

Windows only: the standalone executable bundles the Python runtime, the UI
assets and the WebView2 host.  There is no Linux equivalent - PyInstaller
cannot reliably bundle GTK and WebKitGTK, so the app runs from source there
(see the README).

Build:
  pyinstaller agent_monitor_for_claude.spec
"""

a = Analysis(
    ['agent_monitor_for_claude/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('locale/*.json', 'locale'),
        ('pricing.json', '.'),
        ('agent_monitor_for_claude/ui/index.html', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/index.css', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/boot.js', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/boot-scripts.js', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/logic.js', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/index.js', 'agent_monitor_for_claude/ui'),
        # ui/dev-mock.js is intentionally NOT bundled: it holds the browser
        # preview's fabricated session data and must never ship in the app.
    ],
    hiddenimports=[
        # The platform layer dispatches on sys.platform, which PyInstaller
        # cannot follow, so each Windows backend is named explicitly.
        'agent_monitor_for_claude.platforms.win32',
        'agent_monitor_for_claude.platforms.instance_win32',
        'agent_monitor_for_claude.platforms.process_win32',
        'webview',
        'webview.platforms.edgechromium',
        'clr_loader',
        'pythonnet',
        'bottle',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PyInstaller walks both branches of the platform dispatch; excluding the
        # Linux backends keeps the EXE small and avoids pulling in POSIX-only
        # modules (fcntl) and the GTK bindings (gi) that never exist here.
        'agent_monitor_for_claude.platforms.linux',
        'agent_monitor_for_claude.platforms.instance_linux',
        'agent_monitor_for_claude.platforms.process_linux',
        'agent_monitor_for_claude.platforms.x11',
        'fcntl', 'gi',
        'unittest', 'test',
        'tkinter', '_tkinter',
        'pydoc', 'xmlrpc',
        'sqlite3',
        'setuptools', '_distutils_hack',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AgentMonitorForClaude',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon='agent_monitor_for_claude.ico',
    version='version_info.py',
)
