"""
WSL Discovery
=============

Isolates every WSL side effect this application ever performs, per the
repo's one-module-per-side-effect rule.  WSL is a Windows feature, so on a
Linux host :func:`wsl_roots` returns an empty list before touching anything
at all - a Linux machine runs its Claude Code sessions natively, under the
local root, and nothing below this line ever runs there.

Two guarantees this module exists to uphold, both load-bearing for the app's
read-only, no-network posture:

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

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

from . import procfs
from .paths import SessionRoot, claude_temp_dir
from .platforms import IS_WINDOWS, no_window_kwargs
from .procinfo import ChildProcessStat, ProcessInfo
from .process_probe import vmmem_present as _vmmem_present
from .settings import WSL_MONITORING

__all__ = ['wsl_roots', 'probe_wsl_sessions', 'wsl_process_stats', 'reset_caches']

# Absolute path to wsl.exe. A relative name would resolve through the Win32
# process-creation search order, which checks the application's own directory
# and the process's current directory BEFORE System32 - so a wsl.exe planted
# next to the downloaded executable would be run silently. SystemRoot always
# names the Windows directory; the fallback covers a stripped environment.
_WSL_EXE = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'wsl.exe')

# Base UNC host WSL exposes every distro's filesystem under - the same host
# paths.host_path routes a distro's own reported paths through.
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
    this is not a Windows host (WSL is a Windows feature; a Linux machine runs
    its Claude Code sessions natively, under the local root), whenever
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
    if not IS_WINDOWS or not WSL_MONITORING:
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
        # no_window_kwargs() suppresses the console window, so none flashes even
        # though this app has none of its own; check=False - the returncode is
        # inspected explicitly below instead of raising.
        result = subprocess.run(
            [_WSL_EXE, '--list', '--running', '--quiet'],
            capture_output=True, timeout=5, check=False, **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    return _parse_distro_list(result.stdout)


def _parse_distro_list(raw: bytes) -> list[str]:
    """Decode ``wsl.exe``'s output into a list of distro names.

    ``wsl.exe`` writes UTF-16-LE by default, but honors the documented
    ``WSL_UTF8=1`` environment variable (WSL >= 0.64.0) and writes UTF-8
    then - and the monitor inherits whatever the user set.  UTF-16-LE text
    always interleaves NUL bytes; UTF-8 never contains one, so a NUL sniff
    picks the right decoding either way.  Stray bytes that do not decode
    cleanly are dropped rather than raised, and blank lines (a trailing
    newline, or the whole output when nothing is running) are dropped too.
    """
    encoding = 'utf-16-le' if b'\x00' in raw else 'utf-8'
    text = raw.decode(encoding, errors='ignore')
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
    unreadable to the account the 9P share runs as; a bare ``is_dir()`` would
    let that permission error propagate out of this function and drop the
    *whole* distro via ``_discover_roots``'s outer guard, even when a
    perfectly good ``home/*/.claude`` had already been found. One unreadable
    candidate is skipped on its own instead; its siblings and the rest of the
    distro are unaffected.
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
            claude_temp_dir=claude_temp_dir(distro_base / 'tmp'),
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


def probe_wsl_sessions(root: SessionRoot, requests: Iterable[tuple[int, int | None]]) -> dict[int, ProcessInfo]:
    """Probe WSL session liveness and running children from one procfs scan.

    Reads ``root.proc_dir`` - the distro's own ``/proc`` shared over the 9P/UNC mount - directly
    through :mod:`agent_monitor_for_claude.procfs`, so no subprocess is ever invoked here, matching
    the module's one-command guarantee (see the module docstring). One scan answers every request: a
    pid absent from the table is not alive; a pid present but whose recorded start time (stat field
    22) does not match *proc_start_ticks* means Linux recycled the pid for an unrelated process, so
    it is reported not alive too. ``host`` and ``via_cli`` are always ``None``/``False``: a WSL
    session has no Windows-side ancestry to classify, and the distro name the UI shows for it comes
    from the root's label rather than from a process.

    The tick rate is the kernel default rather than the distro's own: asking for the real value
    would mean running a program inside the distribution, which this module never does. Liveness
    compares tick counts directly and never needs it, so a distro with a non-default rate only
    widens or narrows the session-helper window - it can never make a live session read as dead.

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
        One entry per requested pid. An unreachable *root.proc_dir* (the distro gone, the mount
        dropped) degrades every request to not alive, rather than raising.
    """
    table = procfs.read_proc_table(root.proc_dir) if root.proc_dir is not None else {}
    index = procfs.children_index(table)

    result: dict[int, ProcessInfo] = {}
    for pid, proc_start_ticks in requests:
        descendants = procfs.live_descendants(pid, proc_start_ticks, table, index)
        if descendants is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        result[pid] = ProcessInfo(alive=True, tool_running=bool(descendants), host=None, via_cli=False, child_count=len(descendants))

    return result


def wsl_process_stats(root: SessionRoot, pid: int, proc_start_ticks: int | None) -> list[ChildProcessStat]:
    """Return live CPU / memory / uptime for one WSL session's descendant processes.

    The descendant set and liveness gate are exactly :func:`probe_wsl_sessions`'s - both go through
    ``procfs.live_descendants`` - so the panel lists precisely the processes the badge counts, and a
    dead or stale session yields ``[]`` the same way. Memory and uptime are read straight from the
    one procfs scan; CPU has no absolute reading there, only cumulative ticks, so it is sampled
    against the previous call, which is why the first reading of a freshly seen process is ``None``.
    Unlike ``process_probe.process_stats`` on Windows, no trailing ``wsl_vm`` context row is appended
    here - these rows already are the session's real work, not a Windows-side relay standing in for
    it.

    The page size and tick rate are the kernel defaults, for the same reason
    :func:`probe_wsl_sessions` uses them: reading the distro's own values would mean running a
    program inside it. A distro that differs only skews these figures.

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
    if root.proc_dir is None:
        return []

    table = procfs.read_proc_table(root.proc_dir)
    index = procfs.children_index(table)

    descendants = procfs.live_descendants(pid, proc_start_ticks, table, index)
    if descendants is None:
        return []

    now = time.time()
    btime = procfs.read_btime(root.proc_dir)

    stats: list[ChildProcessStat] = []
    live_pids: set[int] = set()
    for child_pid, name in descendants:
        entry = table.get(child_pid)
        if entry is None:
            continue

        live_pids.add(child_pid)
        rss_bytes = None if entry.rss_pages is None else entry.rss_pages * procfs.DEFAULT_PAGE_SIZE
        uptime = None if btime is None else max(0.0, now - (btime + entry.starttime / procfs.DEFAULT_CLK_TCK))
        cpu = procfs.sample_cpu(root.origin, child_pid, entry.starttime, entry.cpu_ticks, now)
        stats.append(ChildProcessStat(pid=child_pid, name=name, cpu_percent=cpu, rss_bytes=rss_bytes, uptime_seconds=uptime))

    stats.sort(key=lambda stat: (stat.name, stat.pid))
    procfs.prune_sample_cache(root.origin, live_pids)

    return stats
