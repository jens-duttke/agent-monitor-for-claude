"""
Window Focus
============

Brings the window hosting a session to the foreground when the user clicks
its entry.  The session's live ancestor chain supplies the candidate host
processes; their visible top-level windows are enumerated and, for hosts
that keep several windows in one process (VS Code, JetBrains IDEs), the
window whose title mentions the session's project is preferred.

A session driven through an external terminal can own no window on its process
chain.  On Windows that is the normal case: a classic console window belongs to
a ``conhost.exe`` child of the shell, and the default-terminal handoff routes
the console to a separate Windows Terminal process with no link back to the
shell.  On Linux the terminal emulator usually *is* on the chain, so the
ancestor search finds it.  Either way, when it finds nothing, a fallback matches
the session title - which Claude Code sets as the terminal title - against
windows owned by a known terminal or console host.

A session running inside a WSL distribution has no host-side process at all -
the agent runs inside the distro, so there is no pid and no ancestor chain to
walk in the first place.  ``app.py`` routes those sessions straight to
:func:`focus_terminal_window`, the same title-only match used as the
fallback above, skipping the pid-based search entirely.

Side effects are limited to window enumeration and activation, and run only on
an explicit user click.  Window titles are compared in memory to pick the right
window - never stored, logged, or displayed.  On Linux that enumeration reads
the standard EWMH properties from the X server; a window drawn by a native
Wayland client is out of reach there and simply not found (see
``platforms/x11.py``).

Alongside activation this module holds the user-initiated launch surfaces: the
VS Code deep link, opening a directory in the file manager, and showing a file
selected in its folder.  Each validates its target first - a UUID, a real
directory, a real file - so nothing else can ever be handed to the desktop; the
platform layer performs the call itself.
"""
from __future__ import annotations

import os
import re

from .platforms import activate_window, enum_windows, open_path, open_uri, reveal_file
from .process_probe import IGNORED_ANCESTOR_NAMES, TERMINAL_WINDOW_OWNERS, ancestry, process_names

__all__ = [
    'focus_session_window', 'focus_terminal_window', 'open_directory', 'open_vscode_session',
    'reveal_in_file_manager', 'select_terminal_window', 'select_window', 'vscode_session_url',
]

# Official deep link of the Claude Code VS Code extension (since v2.1.72):
# focuses the tab of an already-open session in the focused VS Code window.
_VSCODE_SESSION_URL = 'vscode://anthropic.claude-code/open?session={session_id}'

# \Z (not $) anchors the very end, so a trailing newline cannot slip a
# non-UUID tail into the launched URI.
_SESSION_ID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')

# Shortest session title still specific enough to match a terminal window by;
# below this a stray short title could raise an unrelated terminal.
_MIN_TERMINAL_TITLE = 3


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
        if ancestor_name not in IGNORED_ANCESTOR_NAMES:
            candidate_pids.append(ancestor_pid)

    windows = enum_windows()
    handle = select_window(windows, candidate_pids, project_name)

    if handle is None:
        handle = select_terminal_window(windows, process_names(), session_title)

    if handle is None:
        return False

    return activate_window(handle)


def focus_terminal_window(session_title: str) -> bool:
    """Bring a session's terminal window to the foreground by its title alone.

    A session running inside a WSL distribution has no host-side process at all
    - the agent runs inside the distro - so there is no pid to search from and
    no window can ever sit on a process chain the way :func:`focus_session_window`
    walks for a local session.  The session's terminal is still a window on this
    side (some terminal emulator hosts it), and Claude Code sets that terminal's
    title to the session title exactly as it does for a local session, so
    matching on the title alone - the same fallback :func:`focus_session_window`
    already uses when the ancestor search finds nothing - is the only route that
    can ever find it.

    Parameters
    ----------
    session_title : str
        The session title shown in the UI, matched against terminal and
        console window titles; see :func:`select_terminal_window`.

    Returns
    -------
    bool
        True if a matching terminal window was found and activated.
    """
    handle = select_terminal_window(enum_windows(), process_names(), session_title)

    if handle is None:
        return False

    return activate_window(handle)


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

    return open_uri(url)


def open_directory(path: str) -> bool:
    """Open an existing local directory in the desktop's file manager (user-initiated).

    Only a real directory is ever handed to the desktop: the path is validated
    with ``os.path.isdir`` first, so a stale path, a file, or anything carrying
    a URI scheme is a no-op rather than something that could be executed.

    Returns
    -------
    bool
        True if an existing directory was opened.
    """
    if not path or not os.path.isdir(path):
        return False

    return open_path(path)


def reveal_in_file_manager(path: str) -> bool:
    """Show an existing file in the file manager, selected in its folder (user-initiated).

    The file is only ever *shown*, never opened: the platform call raises a file-
    manager window on the containing folder with the item selected, so no program
    is launched for the file and its content is never handed to another
    application.  Only a real file reaches that call - the path is validated with
    ``os.path.isfile`` first, so a stale path, a directory, or anything carrying a
    URI scheme is a no-op.  When the call fails (a shell that cannot build an id
    list, a desktop with no file-manager service), the containing folder is opened
    instead: the same window, minus the selection.

    Returns
    -------
    bool
        True if a file-manager window was raised on the file or its folder.
    """
    if not path or not os.path.isfile(path):
        return False

    if reveal_file(path):
        return True

    return open_directory(os.path.dirname(path))


def select_window(windows: list[tuple[int, int, str]], candidate_pids: list[int], project_name: str) -> int | None:
    """Pick the best window for a session (pure decision logic).

    Walks the candidate processes nearest-first.  Within the first process
    that owns visible windows, a title mentioning the project name wins
    (multi-window hosts keep all windows in one process); otherwise the
    process's first window is used.

    Parameters
    ----------
    windows : list of (handle, pid, title)
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
            for handle, _pid, title in owned:
                if needle in title.casefold():
                    return handle

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
    windows : list of (handle, pid, title)
        Visible top-level windows.
    owner_names : dict[int, str]
        Map of window-owner PID to lowercased process name.
    session_title : str
        The session title shown in the UI; empty or too short disables the match.
    """
    needle = session_title.strip().casefold()
    if len(needle) < _MIN_TERMINAL_TITLE:
        return None

    for handle, pid, title in windows:
        if owner_names.get(pid) in TERMINAL_WINDOW_OWNERS and needle in title.casefold():
            return handle

    return None
