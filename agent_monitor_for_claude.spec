# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Agent Monitor for Claude.

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
        ('agent_monitor_for_claude/ui/logic.js', 'agent_monitor_for_claude/ui'),
        ('agent_monitor_for_claude/ui/index.js', 'agent_monitor_for_claude/ui'),
        # ui/dev-mock.js is intentionally NOT bundled: it holds the browser
        # preview's fabricated session data and must never ship in the app.
    ],
    hiddenimports=[
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
