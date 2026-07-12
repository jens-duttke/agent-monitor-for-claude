"""
Process Probe
=============

Isolates the process-introspection side effects.  A single scan of the
process table per snapshot answers, for every session at once: is the process
alive, is a tool currently executing (meaningful child process), which
application hosts it right now, and is it driven through the CLI (a shell
sits between the session process and its GUI host).  Working from one scan
keeps the result current on every poll and avoids per-PID process walks.
Only process names, parent links, and start times are inspected - never
command lines or arguments.

When the registry record carries the original process start time (``procStart``,
.NET ticks of the local wall clock), it is compared against the live process:
a mismatch means Windows recycled the PID for an unrelated process and the
registry entry is stale, so the session is reported as not alive.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import psutil

__all__ = ['TERMINAL_WINDOW_OWNERS', 'ProcessInfo', 'ancestry', 'probe', 'probe_all', 'process_names']

# Child processes that a Claude Code session always owns while idle; their
# presence must not be read as "a tool is running".
_IGNORED_CHILD_NAMES = frozenset({'conhost.exe'})

# Editors that host a session in their own window.
_EDITOR_HOSTS = {
    'code.exe': 'VS Code',
    'code - insiders.exe': 'VS Code Insiders',
    'codium.exe': 'VSCodium',
    'cursor.exe': 'Cursor',
    'windsurf.exe': 'Windsurf',
    'devenv.exe': 'Visual Studio',
    'pycharm64.exe': 'PyCharm',
    'idea64.exe': 'IntelliJ IDEA',
    'webstorm64.exe': 'WebStorm',
    'phpstorm64.exe': 'PhpStorm',
    'rider64.exe': 'Rider',
    'clion64.exe': 'CLion',
    'goland64.exe': 'GoLand',
    'rubymine64.exe': 'RubyMine',
    'datagrip64.exe': 'DataGrip',
}

# Terminal emulators that host a session in their own window.
_TERMINAL_HOSTS = {
    'windowsterminal.exe': 'Windows Terminal',
    'wezterm-gui.exe': 'WezTerm',
    'alacritty.exe': 'Alacritty',
    'conemu64.exe': 'ConEmu',
    'conemu.exe': 'ConEmu',
    'hyper.exe': 'Hyper',
    'warp.exe': 'Warp',
    'tabby.exe': 'Tabby',
    'mintty.exe': 'Terminal',
}

# GUI hosts (editors and terminal emulators).
_GUI_HOSTS = {**_EDITOR_HOSTS, **_TERMINAL_HOSTS}

# Console hosts own the window of a classic console session (a child of the
# shell); a terminal emulator owns the window of a session handed to it through
# Windows' default-terminal mechanism, which leaves no process link back to the
# shell.  Either way the window is not on the session's process chain, so
# focusing it relies on matching the session title against these owners.
_CONSOLE_HOST_NAMES = frozenset({'conhost.exe', 'openconsole.exe'})
TERMINAL_WINDOW_OWNERS = frozenset(_TERMINAL_HOSTS) | _CONSOLE_HOST_NAMES

# Shells; one appearing between the session process and its GUI host means
# the session is driven through the CLI rather than the editor extension.
_SHELL_HOSTS = {
    'pwsh.exe': 'PowerShell',
    'powershell.exe': 'PowerShell',
    'cmd.exe': 'Command Prompt',
    'bash.exe': 'Git Bash',
}

_MAX_ANCESTOR_DEPTH = 15


@dataclass(frozen=True)
class ProcessInfo:
    """Result of probing a session's process."""

    alive: bool
    tool_running: bool
    host: str | None = None
    via_cli: bool = False
    child_count: int = 0
    child_names: tuple[str, ...] = ()


def probe_all(requests: Iterable[tuple[int, int | None]]) -> dict[int, ProcessInfo]:
    """Probe many sessions from one process-table scan.

    Parameters
    ----------
    requests : iterable of (pid, proc_start_ticks)
        Process IDs from the session registry, each with the recorded process
        start time (.NET local-time ticks) or ``None``.

    Returns
    -------
    dict[int, ProcessInfo]
        One entry per requested PID.
    """
    table = _scan_processes()

    children_index: dict[int, list[int]] = {}
    for pid, (ppid, _name) in table.items():
        children_index.setdefault(ppid, []).append(pid)

    # Start times are queried lazily and only for session processes and their
    # ancestors - fetching them for the whole table would need an expensive
    # OpenProcess per running process.
    create_time_cache: dict[int, float | None] = {}

    result: dict[int, ProcessInfo] = {}
    for pid, proc_start_ticks in requests:
        entry = table.get(pid)

        if entry is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        create_time = _create_time(pid, create_time_cache)
        if create_time is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        if proc_start_ticks and not _ticks_match_epoch(proc_start_ticks, create_time):
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        ancestor_names = [name for _pid, name in _ancestors(pid, table, create_time_cache)]
        host, via_cli = _classify_ancestry(ancestor_names)
        children = _meaningful_children(pid, table, children_index, create_time_cache)
        result[pid] = ProcessInfo(
            alive=True,
            tool_running=bool(children),
            host=host,
            via_cli=via_cli,
            child_count=len(children),
            child_names=tuple(sorted(set(children))),
        )

    return result


def probe(pid: int, proc_start_ticks: int | None = None) -> ProcessInfo:
    """Probe a single session process (convenience wrapper around ``probe_all``)."""
    return probe_all([(pid, proc_start_ticks)])[pid]


def ancestry(pid: int) -> list[tuple[int, str]]:
    """Return the live ancestor chain of *pid* as ``(pid, name_lower)``, nearest first."""
    table = _scan_processes()
    create_time_cache: dict[int, float | None] = {}
    return _ancestors(pid, table, create_time_cache)


def process_names() -> dict[int, str]:
    """Return ``{pid: lowercased executable name}`` for every running process."""
    return {pid: name for pid, (_ppid, name) in _scan_processes().items()}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.wintypes.DWORD),
        ('cntUsage', ctypes.wintypes.DWORD),
        ('th32ProcessID', ctypes.wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', ctypes.wintypes.DWORD),
        ('cntThreads', ctypes.wintypes.DWORD),
        ('th32ParentProcessID', ctypes.wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', ctypes.wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# A private kernel32 handle with explicit prototypes.  Without a HANDLE restype
# the snapshot would marshal through the default c_int, truncating a 64-bit
# handle and letting a failed call slip past the _INVALID_HANDLE_VALUE guard.
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE

_kernel32.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL

_kernel32.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = ctypes.wintypes.BOOL

_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL


def _scan_processes() -> dict[int, tuple[int, str]]:
    """Return the current process table as ``pid -> (ppid, name_lower)``.

    Uses one Toolhelp snapshot (a single kernel call) instead of opening a
    handle per process - enumerating names via per-process handles takes
    seconds under antivirus scrutiny, the snapshot takes milliseconds.
    """
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE or not snapshot:
        return {}

    table: dict[int, tuple[int, str]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)

        if _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile.lower())
                if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)

    return table


def _create_time(pid: int, cache: dict[int, float | None]) -> float | None:
    """Return a process start time (epoch seconds), cached per scan."""
    if pid not in cache:
        try:
            cache[pid] = psutil.Process(pid).create_time()
        except psutil.Error:
            cache[pid] = None

    return cache[pid]


def _ancestors(pid: int, table: dict[int, tuple[int, str]], create_time_cache: dict[int, float | None]) -> list[tuple[int, str]]:
    """Return ancestors as ``(pid, name_lower)``, nearest first, guarding against PID reuse.

    A parent whose start time is later than its child's cannot be the real
    parent (Windows recycled the PID), so the walk stops there.  Unknown start
    times leave the link accepted - the guard is a heuristic.
    """
    ancestors: list[tuple[int, str]] = []
    visited = {pid}
    current = pid

    for _ in range(_MAX_ANCESTOR_DEPTH):
        entry = table.get(current)
        if entry is None:
            break

        parent_pid = entry[0]
        parent = table.get(parent_pid)
        if parent is None or parent_pid in visited:
            break

        parent_start = _create_time(parent_pid, create_time_cache)
        child_start = _create_time(current, create_time_cache)
        if parent_start is not None and child_start is not None and parent_start > child_start + 1.0:
            break

        ancestors.append((parent_pid, parent[1]))
        visited.add(parent_pid)
        current = parent_pid

    return ancestors


def _classify_ancestry(ancestor_names: list[str]) -> tuple[str | None, bool]:
    """Derive (host, via_cli) from ancestor names ordered nearest first.

    The first GUI host on the chain labels where the session runs right now;
    a shell encountered before it means the session is driven through the CLI.
    Without any GUI host, the nearest shell itself is the host.
    """
    first_shell: str | None = None

    for name in ancestor_names:
        if name in _GUI_HOSTS:
            return _GUI_HOSTS[name], first_shell is not None
        if first_shell is None and name in _SHELL_HOSTS:
            first_shell = _SHELL_HOSTS[name]

    if first_shell is not None:
        return first_shell, True

    return None, False


def _meaningful_children(
    pid: int,
    table: dict[int, tuple[int, str]],
    children_index: dict[int, list[int]],
    create_time_cache: dict[int, float | None],
) -> list[str]:
    """Return the names of the genuine process tree below *pid*, excluding the console host.

    Every parent -> child link is validated against PID reuse, exactly like the
    ancestor walk: a real child cannot have started before its parent.  This is
    essential because Windows does not reparent orphaned processes - when a
    parent exits, its children keep the dead parent's PID, and once that PID is
    recycled for the session process, every unrelated orphan (system services
    started at boot, and their whole subtree) would otherwise be counted as a
    child.  A link is accepted only when both start times are known and the
    child is not older than the parent, so those bogus links are pruned before
    the walk can descend into the system process tree.
    """
    names: list[str] = []
    visited = {pid}
    pending: list[tuple[int, int]] = [(pid, child_pid) for child_pid in children_index.get(pid, [])]

    while pending:
        parent_pid, child_pid = pending.pop()
        if child_pid in visited:
            continue
        if not _is_child_link_real(parent_pid, child_pid, create_time_cache):
            continue
        visited.add(child_pid)

        entry = table.get(child_pid)
        if entry is None:
            continue
        if entry[1] not in _IGNORED_CHILD_NAMES:
            names.append(entry[1])

        pending.extend((child_pid, grandchild) for grandchild in children_index.get(child_pid, []))

    return names


def _is_child_link_real(parent_pid: int, child_pid: int, create_time_cache: dict[int, float | None]) -> bool:
    """Return True if *child_pid* can genuinely be a child of *parent_pid*.

    Guards against PID reuse via start times: a real child starts at or after
    its parent.  Both times must be known - an unverifiable link is rejected,
    since the only descendant links we cannot query belong to protected system
    processes that are never real children of a Claude Code session.
    """
    parent_start = _create_time(parent_pid, create_time_cache)
    child_start = _create_time(child_pid, create_time_cache)
    if parent_start is None or child_start is None:
        return False

    return child_start >= parent_start - 1.0


def _ticks_match_epoch(ticks: int, epoch_seconds: float, tolerance_seconds: float = 10.0) -> bool:
    """Compare .NET local-time ticks against a Unix timestamp.

    ``procStart`` holds the local wall-clock time as .NET ticks (100 ns units
    since year 1); ``epoch_seconds`` is converted to the same local wall clock
    for the comparison.  A corrupted registry value can push the tick count past
    the representable date range, so the conversion degrades to a mismatch (the
    session is then reported as not alive) instead of crashing the snapshot.
    """
    try:
        recorded = datetime(1, 1, 1) + timedelta(microseconds=ticks / 10)
        actual = datetime.fromtimestamp(epoch_seconds)
    except (OverflowError, OSError, ValueError):
        return False

    return abs((recorded - actual).total_seconds()) <= tolerance_seconds
