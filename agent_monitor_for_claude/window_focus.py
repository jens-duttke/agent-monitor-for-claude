"""
Window Focus
============

Brings the window hosting a session to the foreground when the user clicks
its entry.  The session's live ancestor chain supplies the candidate host
processes; their visible top-level windows are enumerated and, for hosts
that keep several windows in one process (VS Code, JetBrains IDEs), the
window whose title mentions the session's project is preferred.

A session driven through an external terminal owns no window on its process
chain: a classic console window belongs to a ``conhost.exe`` child of the
shell, and Windows' default-terminal handoff routes the console to a separate
Windows Terminal process with no link back to the shell.  For those the
ancestor search finds nothing, so a fallback matches the session title - which
Claude Code sets as the terminal title - against windows owned by a known
terminal or console host.

Side effects are limited to Win32 window enumeration and activation, and run
only on an explicit user click.  Window titles are compared in memory to pick
the right window - never stored, logged, or displayed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import re

from .process_probe import TERMINAL_WINDOW_OWNERS, ancestry, process_names

__all__ = ['focus_session_window', 'open_directory', 'open_vscode_session', 'vscode_session_url']

# Official deep link of the Claude Code VS Code extension (since v2.1.72):
# focuses the tab of an already-open session in the focused VS Code window.
_VSCODE_SESSION_URL = 'vscode://anthropic.claude-code/open?session={session_id}'

# \Z (not $) anchors the very end, so a trailing newline cannot slip a
# non-UUID tail into the launched URI.
_SESSION_ID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')

_user32 = ctypes.windll.user32

_SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002

# Ancestors that own windows for the whole desktop, never for one session.
_IGNORED_ANCESTOR_NAMES = frozenset({'explorer.exe'})

# Shortest session title still specific enough to match a terminal window by;
# below this a stray short title could raise an unrelated terminal.
_MIN_TERMINAL_TITLE = 3

_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def focus_session_window(pid: int, project_name: str, session_title: str = '') -> bool:
    """Bring the window hosting the session process *pid* to the foreground.

    The window owned by the session or one of its ancestors is preferred (this
    covers editors and terminals that sit on the process chain).  When none is
    found - a session driven through an external terminal - *session_title* is
    matched against terminal and console windows as a fallback.

    Returns
    -------
    bool
        True if a window was found and activated.
    """
    candidate_pids = [pid]
    for ancestor_pid, ancestor_name in ancestry(pid):
        if ancestor_name not in _IGNORED_ANCESTOR_NAMES:
            candidate_pids.append(ancestor_pid)

    windows = _enum_windows()
    hwnd = select_window(windows, candidate_pids, project_name)

    if hwnd is None:
        hwnd = select_terminal_window(windows, process_names(), session_title)

    if hwnd is None:
        return False

    return _activate(hwnd)


def vscode_session_url(session_id: str) -> str | None:
    """Return the extension deep-link URL for a session id, or None if invalid.

    The id must be a UUID - strict validation keeps the launched URI fully
    predictable (no other schemes, no extra parameters).
    """
    if not session_id or not _SESSION_ID_PATTERN.match(session_id):
        return None

    return _VSCODE_SESSION_URL.format(session_id=session_id.lower())


def open_vscode_session(session_id: str) -> bool:
    """Focus a session's tab via the official VS Code extension deep link."""
    url = vscode_session_url(session_id)
    if url is None:
        return False

    try:
        os.startfile(url)
    except OSError:
        return False

    return True


def open_directory(path: str) -> bool:
    """Open an existing local directory in Windows Explorer (user-initiated).

    Only a real directory is ever handed to the shell: the path is validated
    with ``os.path.isdir`` first, so a stale path, a file, or anything carrying
    a URI scheme is a no-op rather than something the shell might execute.  For
    a folder, ``os.startfile`` is routed to Explorer by the shell.

    Returns
    -------
    bool
        True if an existing directory was opened.
    """
    if not path or not os.path.isdir(path):
        return False

    try:
        os.startfile(path)
    except OSError:
        return False

    return True


def select_window(windows: list[tuple[int, int, str]], candidate_pids: list[int], project_name: str) -> int | None:
    """Pick the best window for a session (pure decision logic).

    Walks the candidate processes nearest-first.  Within the first process
    that owns visible windows, a title mentioning the project name wins
    (multi-window hosts keep all windows in one process); otherwise the
    process's first window is used.

    Parameters
    ----------
    windows : list of (hwnd, pid, title)
        Visible top-level windows.
    candidate_pids : list of int
        Session process and its ancestors, nearest first.
    project_name : str
        Project folder name used for title matching.
    """
    needle = project_name.casefold()

    for candidate_pid in candidate_pids:
        owned = [window for window in windows if window[1] == candidate_pid]
        if not owned:
            continue

        if needle:
            for hwnd, _pid, title in owned:
                if needle in title.casefold():
                    return hwnd

        return owned[0][0]

    return None


def select_terminal_window(windows: list[tuple[int, int, str]], owner_names: dict[int, str], session_title: str) -> int | None:
    """Pick a terminal or console window carrying the session title (pure decision logic).

    Claude Code sets the terminal title to the session title, which the
    terminal reflects in its window title, so a window whose title contains the
    session title is the session's terminal.  The search is confined to windows
    owned by a known terminal or console host, so an unrelated window that
    merely shares the text is never raised.

    Parameters
    ----------
    windows : list of (hwnd, pid, title)
        Visible top-level windows.
    owner_names : dict[int, str]
        Map of window-owner PID to lowercased process name.
    session_title : str
        The session title shown in the UI; empty or too short disables the match.
    """
    needle = session_title.strip().casefold()
    if len(needle) < _MIN_TERMINAL_TITLE:
        return None

    for hwnd, pid, title in windows:
        if owner_names.get(pid) in TERMINAL_WINDOW_OWNERS and needle in title.casefold():
            return hwnd

    return None


def _enum_windows() -> list[tuple[int, int, str]]:
    """Return all visible, titled top-level windows as ``(hwnd, pid, title)``."""
    windows: list[tuple[int, int, str]] = []

    def _collect(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True

        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)

        window_pid = ctypes.wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))

        windows.append((hwnd, window_pid.value, buffer.value))
        return True

    _user32.EnumWindows(_EnumWindowsProc(_collect), 0)
    return windows


def _activate(hwnd: int) -> bool:
    """Restore and raise a window to the foreground."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)

    if _user32.SetForegroundWindow(hwnd):
        return True

    # Windows refuses foreground changes in some states; a synthetic ALT tap
    # is the documented workaround to lift that restriction.
    _user32.keybd_event(_VK_MENU, 0, 0, 0)
    result = _user32.SetForegroundWindow(hwnd)
    _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)

    return bool(result)
