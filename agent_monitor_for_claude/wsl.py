"""
WSL Discovery
=============

Isolates every WSL side effect this application ever performs, per the
repo's one-module-per-side-effect rule.  Two guarantees this module exists
to uphold, both load-bearing for the app's read-only, no-network posture:

1. The only program this module - or the application as a whole - ever
   executes is ``wsl.exe --list --running --quiet``: fixed arguments, a
   hidden console window (``CREATE_NO_WINDOW``), used strictly to enumerate.
   Nothing is ever run inside a distribution.
2. A distro absent from that call's output is never touched by any
   filesystem access.  Opening ``\\\\wsl.localhost\\<distro>\\...`` for a
   *stopped* distro starts it - a UNC read is not read-only from the
   distro's point of view - so every ``.claude`` lookup below is gated on
   the running-distro list first, never on a bare scan of the UNC root that
   could stat a stopped one.

Two short-lived caches keep the steady-state cost near zero while WSL is not
in use: a ``vmmem*`` process (the shared WSL2 utility VM) must be seen before
``wsl.exe`` is ever invoked at all (``_VMMEM_TTL``), and the running-distro
list itself is cached a little longer, once that is true (``_DISCOVERY_TTL``).
The moment a check finds ``vmmem`` gone, both caches are dropped immediately -
a VM shutdown (and every distro that ran under it) reads as gone on the very
next poll rather than lingering for the discovery cache window.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

from .paths import SessionRoot
from .process_probe import ProcessInfo, SESSION_HELPER_WINDOW_SECONDS, vmmem_present as _vmmem_present
from .settings import WSL_MONITORING

__all__ = ['wsl_roots', 'probe_wsl_sessions', 'reset_caches']

# Base UNC host WSL exposes every distro's filesystem under - the same host
# paths.wsl_path_to_windows routes a distro's own reported paths through.
# Deliberately a plain string, not a Path: pathlib only recognizes a UNC root
# when the server and the share appear together in one parse, so a bare
# "\\wsl.localhost" Path (no share yet - the share is the distro name, only
# known per-call) silently collapses to a single leading backslash the
# instant it is constructed, and no Path built from it can ever regain the
# doubled separator afterwards. See _distro_roots, which joins server and
# share into one string before Path() first sees either.
_UNC_BASE = r'\\wsl.localhost'

# How long a vmmem*-presence reading is trusted before the process table is
# scanned again.
_VMMEM_TTL = 5.0

# How long the running-distro list is trusted before wsl.exe is invoked again.
_DISCOVERY_TTL = 10.0

# Guards both caches below; released while the underlying probe itself runs,
# so a cache hit on one thread never blocks behind a slow refresh on another.
_cache_lock = threading.Lock()
_vmmem_cache: tuple[float, bool] | None = None
_discovery_cache: tuple[float, list[str]] | None = None


def wsl_roots() -> list[SessionRoot]:
    """Return one ``SessionRoot`` per running WSL distro that has a ``.claude`` directory.

    Returns ``[]`` without any subprocess call or filesystem access whenever
    ``settings.WSL_MONITORING`` is off, or whenever no ``vmmem*`` process is
    running - no WSL2 utility VM means no distro can be running either.
    Otherwise the running-distro list is discovered (both checks cached, see
    the module docstring) and turned into roots by :func:`_discover_roots`. A
    distro absent from that running list is never touched by any filesystem
    access.

    Returns
    -------
    list[SessionRoot]
        One entry per discovered ``.claude`` directory, sorted by distro name
        for a stable fingerprint across polls.
    """
    if not WSL_MONITORING:
        return []

    if not _vmmem_present_cached():
        return []

    distros = _list_running_distros_cached()
    return _discover_roots(distros, _UNC_BASE)


def reset_caches() -> None:
    """Test hook: drop both TTL caches so the next call re-probes from scratch."""
    global _vmmem_cache, _discovery_cache
    with _cache_lock:
        _vmmem_cache = None
        _discovery_cache = None


def _vmmem_present_cached() -> bool:
    """Return whether a ``vmmem*`` process exists, refreshed at most every ``_VMMEM_TTL`` seconds.

    A check that finds vmmem gone also drops the discovery cache immediately,
    so a shut-down WSL2 VM does not leave a stale distro list served for the
    rest of the discovery TTL.
    """
    global _vmmem_cache, _discovery_cache

    with _cache_lock:
        if _vmmem_cache is not None and time.monotonic() - _vmmem_cache[0] < _VMMEM_TTL:
            return _vmmem_cache[1]

    present = _vmmem_present()

    with _cache_lock:
        _vmmem_cache = (time.monotonic(), present)
        if not present:
            _discovery_cache = None

    return present


def _list_running_distros_cached() -> list[str]:
    """Return the running-distro list, refreshed at most every ``_DISCOVERY_TTL`` seconds."""
    global _discovery_cache

    with _cache_lock:
        if _discovery_cache is not None and time.monotonic() - _discovery_cache[0] < _DISCOVERY_TTL:
            return _discovery_cache[1]

    distros = _list_running_distros()

    with _cache_lock:
        _discovery_cache = (time.monotonic(), distros)

    return distros


def _list_running_distros() -> list[str]:
    """Run the one sanctioned WSL command and parse its output into distro names.

    Fixed arguments, a hidden window, and a short timeout; any failure at all
    (WSL not installed, the call hanging, a nonzero exit) degrades to an empty
    list rather than raising - this is a best-effort enumeration, never a
    required capability, and the caller treats "no distros" and "WSL
    unusable" identically.
    """
    try:
        # creationflags 0x08000000 is CREATE_NO_WINDOW, so no console flashes
        # even though this app has none of its own; check=False - the
        # returncode is inspected explicitly below instead of raising.
        result = subprocess.run(
            ['wsl.exe', '--list', '--running', '--quiet'],
            capture_output=True, timeout=5, creationflags=0x08000000, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    return _parse_distro_list(result.stdout)


def _parse_distro_list(raw: bytes) -> list[str]:
    """Decode ``wsl.exe``'s output into a list of distro names.

    ``wsl.exe`` writes UTF-16-LE regardless of the console code page; stray
    bytes that do not decode cleanly are dropped rather than raised, and blank
    lines (a trailing newline, or the whole output when nothing is running)
    are dropped too.
    """
    text = raw.decode('utf-16-le', errors='ignore')
    return [line.strip() for line in text.splitlines() if line.strip()]


def _discover_roots(distros: list[str], unc_base: Path | str) -> list[SessionRoot]:
    """Build a ``SessionRoot`` for every ``.claude`` directory found under each running distro.

    Pure given *unc_base* - production passes ``_UNC_BASE`` (a bare host
    string), tests inject a temp directory ``Path`` standing in for it; either
    works, see ``_distro_roots``.  *distros* is trusted to already be the
    running-only list: a name absent from it is never looked up here, however
    it happens to sit on disk - this is what keeps a stopped distro untouched.
    Distro names are processed in sorted order for a stable fingerprint across
    polls.  Any ``OSError`` while probing one distro (a dropped UNC connection,
    a permission error) drops that distro entirely rather than returning a
    partial result for it; the other distros are unaffected.
    """
    roots: list[SessionRoot] = []
    for distro in sorted(distros):
        try:
            roots.extend(_distro_roots(distro, unc_base))
        except OSError:
            continue

    return roots


def _distro_roots(distro: str, unc_base: Path | str) -> list[SessionRoot]:
    """Return every root for one distro: each ``home/*/.claude`` plus ``root/.claude``.

    The first ``.claude`` found keeps the plain ``wsl:<distro>`` origin; each
    further one - more than one user account with Claude Code configured -
    gets a disambiguating ``wsl:<distro>:<home>`` origin, so the common case
    of a single user still gets the stable, undecorated origin.
    """
    # Joined as a string, not via Path.__truediv__: when unc_base is the bare
    # "\\wsl.localhost" host (see _UNC_BASE), this is the first time server
    # and share (the distro name) are ever combined, which is what pathlib
    # requires to recognize the result as a UNC root at all.
    distro_base = Path(str(unc_base) + '\\' + distro)
    candidates: list[tuple[str, Path]] = []

    home_dir = distro_base / 'home'
    if home_dir.is_dir():
        for user_dir in sorted(home_dir.iterdir(), key=lambda entry: entry.name):
            claude_dir = user_dir / '.claude'
            if claude_dir.is_dir():
                candidates.append((user_dir.name, claude_dir))

    root_claude = distro_base / 'root' / '.claude'
    if root_claude.is_dir():
        candidates.append(('root', root_claude))

    roots: list[SessionRoot] = []
    for index, (home_name, claude_dir) in enumerate(candidates):
        origin = f'wsl:{distro}' if index == 0 else f'wsl:{distro}:{home_name}'
        roots.append(SessionRoot(
            origin=origin,
            label=distro,
            config_dir=claude_dir,
            proc_dir=distro_base / 'proc',
            temp_dir=distro_base / 'tmp',
        ))

    return roots


# Linux clock ticks per second, used to interpret /proc/[pid]/stat's tick-based fields (starttime
# here; utime/stime feed later display math). Assumed at the Linux/WSL2 kernel's default of 100 Hz
# rather than queried per distro - reading it (getconf CLK_TCK, or sysconf(_SC_CLK_TCK)) would mean
# running a program inside the distribution, which this module never does (see the module
# docstring). Only the session-helper window below depends on it; liveness compares starttime
# values directly and never needs it, so a distro with a non-default tick rate only widens or
# narrows that window - it can never make a live session read as dead or vice versa.
_CLK_TCK = 100


def probe_wsl_sessions(root: SessionRoot, requests: Iterable[tuple[int, int | None]]) -> dict[int, ProcessInfo]:
    """Probe WSL session liveness and running children from one procfs scan.

    Reads ``root.proc_dir`` - the distro's own ``/proc`` shared over the 9P/UNC mount - directly, so
    no subprocess is ever invoked here, matching the module's one-command guarantee (see the module
    docstring). One scan of the numeric entries under ``root.proc_dir`` answers every request: a pid
    absent from the table is not alive; a pid present but whose recorded start time (stat field 22)
    does not match *proc_start_ticks* means Linux recycled the pid for an unrelated process, so it is
    reported not alive too - the same recycled-pid guard ``process_probe`` applies to the native
    Windows process, adapted to procfs's own start-time field. ``host`` and ``via_cli`` are always
    ``None``/``False``: a WSL session has no Windows-side ancestry to classify the way a native
    session's GUI/shell chain does.

    Parameters
    ----------
    root : SessionRoot
        A WSL root as returned by :func:`wsl_roots`; ``root.proc_dir`` is read.
    requests : iterable of (pid, proc_start_ticks)
        Session process ids to probe, each paired with the recorded start time (``/proc/[pid]/stat``
        field 22) or ``None`` when not yet known.

    Returns
    -------
    dict[int, ProcessInfo]
        One entry per requested pid. Any ``OSError`` while listing *root.proc_dir* (the distro
        unreachable, the mount gone) degrades every request to not alive, rather than raising.
    """
    table = _read_proc_table(root.proc_dir)

    children_index: dict[int, list[int]] = {}
    for pid, (_comm, ppid, _starttime) in table.items():
        children_index.setdefault(ppid, []).append(pid)

    result: dict[int, ProcessInfo] = {}
    for pid, proc_start_ticks in requests:
        entry = table.get(pid)
        if entry is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        _comm, _ppid, starttime = entry
        if proc_start_ticks and proc_start_ticks != starttime:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        descendants = _meaningful_descendants(pid, starttime, table, children_index)
        result[pid] = ProcessInfo(alive=True, tool_running=bool(descendants), host=None, via_cli=False, child_count=len(descendants))

    return result


def _read_proc_table(proc_dir: Path) -> dict[int, tuple[str, int, int]]:
    """Return ``{pid: (comm, ppid, starttime)}`` parsed from every numeric entry under *proc_dir*.

    Any ``OSError`` while listing *proc_dir* (the distro unreachable, the share gone) yields an empty
    table rather than raising; likewise a pid whose own ``stat`` file cannot be read, or whose
    contents do not parse (malformed, or a non-numeric ppid/starttime), is skipped rather than
    aborting the whole scan - one unreadable process must never hide every other session.
    """
    table: dict[int, tuple[str, int, int]] = {}

    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return table

    for entry in entries:
        if not entry.name.isdigit():
            continue

        try:
            text = (entry / 'stat').read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue

        parsed = _parse_stat(text)
        if parsed is None:
            continue
        comm, fields = parsed

        try:
            ppid = int(fields[1])
            starttime = int(fields[19])
        except (IndexError, ValueError):
            continue

        table[int(entry.name)] = (comm, ppid, starttime)

    return table


def _parse_stat(text: str) -> tuple[str, list[str]] | None:
    """Parse one ``/proc/[pid]/stat`` line into ``(comm, fields)``.

    ``comm`` (field 2) is process-settable (e.g. via ``prctl``/``/proc/self/comm``) and may itself
    contain spaces or parentheses - e.g. ``tmux: server (x)`` - so it cannot be delimited by the
    first ``)``. Every field after it is numeric or a single letter, so the *last* ``)`` in the line
    is always the real close; everything between the first ``' ('`` and that closing paren is
    ``comm``, and everything after it, split on whitespace, is *fields*, where ``fields[N - 3]``
    holds stat field ``N`` (fields 1-2, pid and comm, are already consumed above, so field 3, the
    state, lands at ``fields[0]``). Returns ``None`` when the line has no ``)`` at all, or nothing
    shaped like ``' ('`` before it - either way too malformed to trust.
    """
    head, _, tail = text.rpartition(')')
    start = head.find(' (')
    if start == -1:
        return None

    comm = head[start + 2:]
    return comm, tail.split()


def _meaningful_descendants(
    pid: int,
    session_start: int,
    table: dict[int, tuple[str, int, int]],
    children_index: dict[int, list[int]],
) -> list[tuple[int, str]]:
    """Return the process tree below *pid* as ``(pid, comm)``, excluding session-lifetime helpers.

    Mirrors ``process_probe._meaningful_children``: every descendant is visited and its own children
    are always queued for the walk, cycle-guarded with a visited set, but a descendant that started
    within ``SESSION_HELPER_WINDOW_SECONDS`` of *session_start* (a stdio MCP server, a watcher
    started alongside the session) is excluded from the result - the walk continues through it
    regardless, so a genuine tool child spawned later by that helper still counts.
    """
    descendants: list[tuple[int, str]] = []
    visited = {pid}
    pending = list(children_index.get(pid, []))
    helper_window_ticks = SESSION_HELPER_WINDOW_SECONDS * _CLK_TCK

    while pending:
        child_pid = pending.pop()
        if child_pid in visited:
            continue
        visited.add(child_pid)

        entry = table.get(child_pid)
        if entry is None:
            continue
        comm, _ppid, child_start = entry

        if child_start - session_start > helper_window_ticks:
            descendants.append((child_pid, comm))

        pending.extend(children_index.get(child_pid, []))

    return descendants
