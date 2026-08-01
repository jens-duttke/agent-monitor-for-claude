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
``wsl.exe`` is ever invoked at all (``_VMMEM_TTL``), and the discovered root
list - the running-distro list *and* the UNC globbing that turns it into
``SessionRoot`` objects - is cached a little longer, once that is true
(``_DISCOVERY_TTL``). Caching the roots, not just the distro names, matters
because :func:`wsl_roots` is called roughly once a second (the UI's
fingerprint poll, plus every bridge call): without it, every one of those
calls would re-run the per-distro globbing (a home ``is_dir()``, an
``iterdir()``, and an ``is_dir()`` per ``.claude`` candidate) regardless of
the distro list itself being cached. The moment a check finds ``vmmem`` gone,
both caches are dropped immediately - a VM shutdown (and every distro that ran
under it) reads as gone on the very next poll rather than lingering for the
discovery cache window.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

from .paths import SessionRoot
from .process_probe import ChildProcessStat, ProcessInfo, SESSION_HELPER_WINDOW_SECONDS, vmmem_present as _vmmem_present
from .settings import WSL_MONITORING

__all__ = ['wsl_roots', 'probe_wsl_sessions', 'wsl_process_stats', 'reset_caches']

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
_discovery_cache: tuple[float, list[SessionRoot]] | None = None


def wsl_roots() -> list[SessionRoot]:
    """Return one ``SessionRoot`` per running WSL distro that has a ``.claude`` directory.

    Returns ``[]`` without any subprocess call or filesystem access whenever
    ``settings.WSL_MONITORING`` is off, or whenever no ``vmmem*`` process is
    running - no WSL2 utility VM means no distro can be running either.
    Otherwise the discovered root list is served from cache when fresh, or
    rediscovered via :func:`_discover_roots` (both checks cached, see the
    module docstring). A distro absent from the running list is never touched
    by any filesystem access.

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

    return _discovered_roots_cached()


def reset_caches() -> None:
    """Test hook: drop both TTL caches so the next call re-probes from scratch."""
    global _vmmem_cache, _discovery_cache
    with _cache_lock:
        _vmmem_cache = None
        _discovery_cache = None


def _vmmem_present_cached() -> bool:
    """Return whether a ``vmmem*`` process exists, refreshed at most every ``_VMMEM_TTL`` seconds.

    A check that finds vmmem gone also drops the discovery cache immediately,
    so a shut-down WSL2 VM does not leave stale roots served for the rest of
    the discovery TTL.
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


def _discovered_roots_cached() -> list[SessionRoot]:
    """Return the discovered ``SessionRoot`` list, refreshed at most every ``_DISCOVERY_TTL`` seconds.

    Caches the roots themselves, not just the running-distro name list that produces them:
    :func:`_discover_roots`'s UNC globbing (a ``home`` directory ``is_dir()``, an ``iterdir()``, then an
    ``is_dir()`` per candidate ``.claude``) costs several 9P round trips per distro, and this function is
    called roughly once a second (:func:`wsl_roots` is reached from the UI's per-second fingerprint poll,
    plus every bridge call) - re-running that glob on every call would defeat the point of caching at
    all. A cache miss re-lists the running distros and rediscovers their roots together, so the two never
    drift out of step with each other.
    """
    global _discovery_cache

    with _cache_lock:
        if _discovery_cache is not None and time.monotonic() - _discovery_cache[0] < _DISCOVERY_TTL:
            return _discovery_cache[1]

    distros = _list_running_distros()
    roots = _discover_roots(distros, _UNC_BASE)

    with _cache_lock:
        _discovery_cache = (time.monotonic(), roots)

    return roots


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
    polls.

    Two layers of defense against a bad filesystem read, neither of which is
    expected to raise out of here in practice: :func:`_distro_roots` itself
    skips one unreadable *candidate* (a permission error on one user's
    ``.claude``, or on ``root/.claude`` - routine, since ``/root`` is rarely
    readable by the account the 9P share runs as) via :func:`_is_readable_dir`,
    without affecting its siblings or the rest of the distro.  The
    ``except OSError`` here is the outer net for a failure severe enough to
    not be scoped to one candidate (the whole UNC connection to a distro
    dropping mid-call) - it drops only that one distro; the other distros are
    unaffected.
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

    Every directory check here - including the ``home`` gate and each user's
    listing - goes through :func:`_is_readable_dir` rather than a bare
    ``Path.is_dir()``, which only swallows not-found-style errors and lets a
    permission error through. ``root/.claude`` in particular is routinely
    unreadable to the account the 9P share runs as (a real-machine bug: it
    used to raise ``PermissionError``, which propagated out of this function
    and dropped the *whole* distro via ``_discover_roots``'s outer guard,
    even when a perfectly good ``home/*/.claude`` had already been found).
    One unreadable candidate is now skipped on its own; its siblings and the
    rest of the distro are unaffected.
    """
    # Joined as a string, not via Path.__truediv__: when unc_base is the bare
    # "\\wsl.localhost" host (see _UNC_BASE), this is the first time server
    # and share (the distro name) are ever combined, which is what pathlib
    # requires to recognize the result as a UNC root at all.
    distro_base = Path(str(unc_base) + '\\' + distro)
    candidates: list[tuple[str, Path]] = []

    home_dir = distro_base / 'home'
    if _is_readable_dir(home_dir):
        try:
            user_dirs = sorted(home_dir.iterdir(), key=lambda entry: entry.name)
        except OSError:
            user_dirs = []

        for user_dir in user_dirs:
            claude_dir = user_dir / '.claude'
            if _is_readable_dir(claude_dir):
                candidates.append((user_dir.name, claude_dir))

    root_claude = distro_base / 'root' / '.claude'
    if _is_readable_dir(root_claude):
        candidates.append(('root', root_claude))

    roots: list[SessionRoot] = []
    for index, (home_name, claude_dir) in enumerate(candidates):
        # If the candidate that currently holds this plain origin later
        # disappears or turns unreadable, the next poll's index-0 candidate
        # shifts up and inherits it - a UI-held origin from before the shift
        # then resolves to a different user's root. That is never a silent
        # cross-user mix: every action re-derives its target from the session
        # id (a UUID) and cwd, confined to that root's own projects/ tree, so
        # a stale origin just fails to find its file and refuses, rather than
        # reading or deleting the new user's session. Accepted for now.
        origin = f'wsl:{distro}' if index == 0 else f'wsl:{distro}:{home_name}'
        roots.append(SessionRoot(
            origin=origin,
            label=distro,
            config_dir=claude_dir,
            proc_dir=distro_base / 'proc',
            temp_dir=distro_base / 'tmp',
        ))

    return roots


def _is_readable_dir(path: Path) -> bool:
    """Return whether *path* is a directory this process can actually read.

    ``Path.is_dir()`` on its own only swallows not-found-style errors (a
    missing path, a broken symlink) - a permission error (``PermissionError``,
    WinError 5) propagates instead.  That is routine here: a UNC 9P share
    exposes every distro's filesystem including directories this process's
    account cannot read (``/root``, another user's home), so every candidate
    check in :func:`_distro_roots` goes through this helper instead of a bare
    ``is_dir()`` call, catching ``OSError`` - permission errors included - and
    reporting the candidate as simply not usable rather than letting the
    error escape and take the whole distro down with it.
    """
    try:
        return path.is_dir()
    except OSError:
        return False


# Linux clock ticks per second, used to interpret /proc/[pid]/stat's tick-based fields (starttime,
# utime, stime here). Assumed at the Linux/WSL2 kernel's default of 100 Hz rather than queried per
# distro - reading it (getconf CLK_TCK, or sysconf(_SC_CLK_TCK)) would mean running a program inside
# the distribution, which this module never does (see the module docstring). Liveness compares
# starttime values directly and never needs it, so a distro with a non-default tick rate only widens
# or narrows the session-helper window and skews the CPU/uptime figures below - it can never make a
# live session read as dead or vice versa.
_CLK_TCK = 100

# Linux's fixed page size (bytes), used to convert /proc/[pid]/stat's RSS field (pages) to bytes.
# Assumed rather than queried for the same reason as _CLK_TCK above: querying it would mean running
# a program inside the distribution.
_PAGE_SIZE = 4096


def probe_wsl_sessions(root: SessionRoot, requests: Iterable[tuple[int, int | None]]) -> dict[int, ProcessInfo]:
    """Probe WSL session liveness and running children from one procfs scan.

    Reads ``root.proc_dir`` - the distro's own ``/proc`` shared over the 9P/UNC mount - directly, so
    no subprocess is ever invoked here, matching the module's one-command guarantee (see the module
    docstring). One scan of the numeric entries under ``root.proc_dir`` answers every request: a pid
    absent from the table is not alive; a pid present but whose recorded start time (stat field 22)
    does not match *proc_start_ticks* means Linux recycled the pid for an unrelated process, so it is
    reported not alive too - the same recycled-pid guard ``process_probe`` applies to the native
    Windows process, adapted to procfs's own start-time field. The comparison only runs when
    *proc_start_ticks* is not ``None``: ``0`` is a legitimate stat-field value (ticks since boot, not
    since some epoch), never a sentinel for "unknown", so it is compared like any other recorded
    start time rather than skipped. ``host`` and ``via_cli`` are always ``None``/``False``: a WSL
    session has no Windows-side ancestry to classify the way a native session's GUI/shell chain does.

    Parameters
    ----------
    root : SessionRoot
        A WSL root as returned by :func:`wsl_roots`; ``root.proc_dir`` is read.
    requests : iterable of (pid, proc_start_ticks)
        Session process ids to probe, each paired with the recorded start time (``/proc/[pid]/stat``
        field 22), or ``None`` when not yet known - absent, not ``0``, which is a real start time.

    Returns
    -------
    dict[int, ProcessInfo]
        One entry per requested pid. Any ``OSError`` while listing *root.proc_dir* (the distro
        unreachable, the mount gone) degrades every request to not alive, rather than raising.
    """
    table = _read_proc_table(root.proc_dir)
    children_index = _children_index(table)

    result: dict[int, ProcessInfo] = {}
    for pid, proc_start_ticks in requests:
        descendants = _live_descendants(pid, proc_start_ticks, table, children_index)
        if descendants is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        result[pid] = ProcessInfo(alive=True, tool_running=bool(descendants), host=None, via_cli=False, child_count=len(descendants))

    return result


def wsl_process_stats(root: SessionRoot, pid: int, proc_start_ticks: int | None) -> list[ChildProcessStat]:
    """Return live CPU / memory / uptime for one WSL session's descendant processes.

    The descendant set and liveness gate are exactly :func:`probe_wsl_sessions`'s - both share
    :func:`_live_descendants` - so the panel lists precisely the processes the badge counts, and a
    dead or stale session yields ``[]`` the same way. Memory and uptime are read straight from the one
    procfs scan, no sampling needed: ``rss_bytes`` is stat field 24 (pages) times the page size, and
    ``uptime_seconds`` is *now* minus the process's absolute start time, derived from the system boot
    time (the ``btime`` line of ``<root.proc_dir>/stat``) plus its own ``starttime`` field converted
    from ticks - ``None`` when ``btime`` cannot be read. CPU has no such absolute reading in procfs,
    only cumulative ticks, so it is sampled the same way ``process_probe`` samples a Windows process:
    the first reading of a freshly seen ``(origin, pid, starttime)`` is ``None``, and a real percentage
    follows once a prior sample exists to diff against (see :func:`_sample_wsl_cpu`). Unlike
    ``process_probe.process_stats``, no trailing ``wsl_vm`` context row is appended here - these rows
    already are the session's real work, not a Windows-side relay standing in for it.

    Parameters
    ----------
    root : SessionRoot
        A WSL root as returned by :func:`wsl_roots`; ``root.proc_dir`` is read, and ``root.origin``
        scopes the CPU sample cache so two distros' panels never share or evict each other's baseline.
    pid : int
        The session process id from the registry.
    proc_start_ticks : int or None
        The recorded process start time (``/proc/[pid]/stat`` field 22); when given, a mismatch means
        the pid was recycled and an empty list is returned, exactly as :func:`probe_wsl_sessions`.

    Returns
    -------
    list[ChildProcessStat]
        One entry per descendant process, ordered by name then pid so the rows stay put across
        refreshes. Empty when the session process is gone or stale.
    """
    table = _read_proc_table(root.proc_dir)
    children_index = _children_index(table)

    descendants = _live_descendants(pid, proc_start_ticks, table, children_index)
    if descendants is None:
        return []

    now = time.time()
    btime = _read_btime(root.proc_dir)

    stats: list[ChildProcessStat] = []
    live_pids: set[int] = set()
    for child_pid, name in descendants:
        entry = table.get(child_pid)
        if entry is None:
            continue
        _comm, _ppid, child_start, rss_pages, cpu_ticks = entry

        live_pids.add(child_pid)
        rss_bytes = None if rss_pages is None else rss_pages * _PAGE_SIZE
        uptime = None if btime is None else max(0.0, now - (btime + child_start / _CLK_TCK))
        cpu = _sample_wsl_cpu(root.origin, child_pid, child_start, cpu_ticks, now)
        stats.append(ChildProcessStat(pid=child_pid, name=name, cpu_percent=cpu, rss_bytes=rss_bytes, uptime_seconds=uptime))

    stats.sort(key=lambda stat: (stat.name, stat.pid))
    _prune_wsl_sample_cache(root.origin, live_pids)
    return stats


def _read_proc_table(proc_dir: Path) -> dict[int, tuple[str, int, int, int | None, int | None]]:
    """Return ``{pid: (comm, ppid, starttime, rss_pages, cpu_ticks)}`` for every numeric entry under *proc_dir*.

    Any ``OSError`` while listing *proc_dir* (the distro unreachable, the share gone) yields an empty
    table rather than raising; likewise a pid whose own ``stat`` file cannot be read, or whose
    ``comm``, ``ppid``, or ``starttime`` do not parse, is skipped rather than aborting the whole scan -
    one unreadable process must never hide every other session. ``rss_pages`` (field 24) and
    ``cpu_ticks`` (``utime`` + ``stime``, fields 14 and 15) feed :func:`wsl_process_stats` alone -
    :func:`probe_wsl_sessions` never reads them - so either degrades to ``None`` on its own rather than
    dropping the whole entry.
    """
    table: dict[int, tuple[str, int, int, int | None, int | None]] = {}

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

        rss_pages = _parse_optional_int(fields, 21)
        utime = _parse_optional_int(fields, 11)
        stime = _parse_optional_int(fields, 12)
        cpu_ticks = None if utime is None or stime is None else utime + stime

        table[int(entry.name)] = (comm, ppid, starttime, rss_pages, cpu_ticks)

    return table


def _parse_optional_int(fields: list[str], index: int) -> int | None:
    """Parse ``fields[index]`` to an int, or ``None`` when the index is out of range or not numeric."""
    try:
        return int(fields[index])
    except (IndexError, ValueError):
        return None


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


def _children_index(table: dict[int, tuple[str, int, int, int | None, int | None]]) -> dict[int, list[int]]:
    """Build ``{parent_pid: [child_pid, ...]}`` from a parsed procfs table."""
    children_index: dict[int, list[int]] = {}
    for pid, (_comm, ppid, _starttime, _rss_pages, _cpu_ticks) in table.items():
        children_index.setdefault(ppid, []).append(pid)

    return children_index


def _live_descendants(
    pid: int,
    proc_start_ticks: int | None,
    table: dict[int, tuple[str, int, int, int | None, int | None]],
    children_index: dict[int, list[int]],
) -> list[tuple[int, str]] | None:
    """Return *pid*'s meaningful descendants (see :func:`_meaningful_descendants`), or ``None`` if not alive.

    The liveness gate is shared verbatim by :func:`probe_wsl_sessions` and :func:`wsl_process_stats`: a
    pid absent from *table*, or one whose recorded ``starttime`` does not match *proc_start_ticks*
    (Linux recycled the pid), is not alive. The comparison only runs when *proc_start_ticks* is not
    ``None`` - ``0`` is a legitimate start time, never a sentinel for "unknown".
    """
    entry = table.get(pid)
    if entry is None:
        return None

    _comm, _ppid, starttime, _rss_pages, _cpu_ticks = entry
    if proc_start_ticks is not None and proc_start_ticks != starttime:
        return None

    return _meaningful_descendants(pid, starttime, table, children_index)


def _meaningful_descendants(
    pid: int,
    session_start: int,
    table: dict[int, tuple[str, int, int, int | None, int | None]],
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
        comm, _ppid, child_start, _rss_pages, _cpu_ticks = entry

        if child_start - session_start > helper_window_ticks:
            descendants.append((child_pid, comm))

        pending.extend(children_index.get(child_pid, []))

    return descendants


def _read_btime(proc_dir: Path) -> int | None:
    """Return the system boot time (epoch seconds) from the ``btime`` line of ``<proc_dir>/stat``.

    Defensive like every other procfs read in this module: a missing or unreadable file, or a response
    with no parseable ``btime`` line, yields ``None`` rather than raising - callers degrade the uptime
    figure to ``None`` rather than letting the whole probe fail.
    """
    try:
        text = (proc_dir / 'stat').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None

    for line in text.splitlines():
        if line.startswith('btime '):
            return _parse_optional_int(line.split(), 1)

    return None


# Live CPU-tick baselines kept between calls so cpu_percent can report the delta since the previous
# sample, mirroring process_probe's _sample_lock/_sample_cache pair. Keyed by (origin, pid) rather
# than pid alone - two distros' proc namespaces reuse the same pid range independently, so the origin
# disambiguates them the same way it disambiguates every other per-root lookup in this application.
_wsl_sample_lock = threading.Lock()
_wsl_sample_cache: dict[tuple[str, int], tuple[int, int, float]] = {}


def _sample_wsl_cpu(origin: str, pid: int, starttime: int, cpu_ticks: int | None, now: float) -> float | None:
    """Return one descendant's CPU percent, sampled against the previous call for the same (origin, pid).

    ``cpu_ticks`` is the process's cumulative ``utime + stime`` at *now* - procfs has no instantaneous
    CPU figure, only this running total, so a percentage needs two readings to diff. The first sighting
    of a given ``(origin, pid, starttime)`` therefore has nothing to diff against and reads ``None``; a
    later call within the same process's lifetime computes the ticks elapsed over the wall time elapsed
    since the previous sample. A cached entry whose ``starttime`` no longer matches means the pid was
    recycled by an unrelated process, so it is treated as an unseen first sighting rather than diffed
    against the old process's ticks. ``cpu_ticks`` itself being ``None`` (the stat fields failed to
    parse) reads as ``None`` and evicts any cached baseline for the key, so a later successful read
    starts over as a clean first sighting rather than diffing across the unreadable gap.
    """
    key = (origin, pid)
    if cpu_ticks is None:
        with _wsl_sample_lock:
            _wsl_sample_cache.pop(key, None)
        return None

    with _wsl_sample_lock:
        cached = _wsl_sample_cache.get(key)
        _wsl_sample_cache[key] = (starttime, cpu_ticks, now)

    if cached is None or cached[0] != starttime:
        return None

    _prev_starttime, prev_ticks, prev_wall = cached
    delta_wall = now - prev_wall
    if delta_wall <= 0:
        return None

    delta_ticks = cpu_ticks - prev_ticks
    return max(0.0, (delta_ticks / _CLK_TCK) / delta_wall * 100.0)


def _prune_wsl_sample_cache(origin: str, live_pids: set[int]) -> None:
    """Drop cached CPU baselines for *origin* whose pid fell out of the current descendant set.

    Scoped to *origin* alone, mirroring ``process_probe._prune_sample_cache`` narrowed per root: two
    distros' (or two disambiguated roots') process panels sample independently, so a pid missing from
    *live_pids* here - simply because this call is for a different origin - must never evict that other
    origin's cached baseline.
    """
    with _wsl_sample_lock:
        for key in list(_wsl_sample_cache):
            if key[0] == origin and key[1] not in live_pids:
                _wsl_sample_cache.pop(key, None)
