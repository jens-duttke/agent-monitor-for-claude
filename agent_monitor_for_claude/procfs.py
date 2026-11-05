"""
Procfs Reader
==============

Reads a Linux ``/proc`` tree and answers the questions the process probe asks
of it: which pids exist, how they are related, when each started, and what CPU
and memory each is using.  Per process, ``/proc/[pid]/stat`` is opened - process
names, parent links, start times, and resource counters - and, for a walk down
from known pids (:func:`read_proc_subtree`), the ``task/[tid]/children`` lists;
never a command line.

Two callers share it, and they differ only in which ``/proc`` they hand in:

* :mod:`agent_monitor_for_claude.platforms.process_linux` reads the running
  system's own ``/proc`` for a native Linux session;
* :mod:`agent_monitor_for_claude.wsl` reads a WSL distro's ``/proc`` over the
  ``\\\\wsl.localhost`` share, from a Windows host.

Every read degrades rather than raises: an unreachable tree yields an empty
table, and one process whose ``stat`` file cannot be read or parsed is skipped
without affecting the rest of the scan.  A single unreadable process must never
hide every other session.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .procinfo import SESSION_HELPER_WINDOW_SECONDS

__all__ = [
    'DEFAULT_CLK_TCK', 'DEFAULT_PAGE_SIZE', 'ProcEntry', 'ancestors', 'children_index', 'clk_tck',
    'live_descendants', 'page_size', 'parse_stat', 'prune_sample_cache', 'read_btime', 'read_proc_subtree',
    'read_proc_table', 'sample_cpu',
]

# Linux clock ticks per second, used to interpret /proc/[pid]/stat's tick-based fields (starttime,
# utime, stime here).  The kernel's long-standing default; :func:`clk_tck` queries the real value
# where the tree being read is the running system's own, and callers reading a *foreign* tree (a WSL
# distro) fall back to this constant rather than running a program inside it to ask.  Liveness
# compares starttime values directly and never needs it, so a mismatched tick rate only widens or
# narrows the session-helper window and skews the CPU/uptime figures - it can never make a live
# session read as dead or vice versa.
DEFAULT_CLK_TCK = 100

# Linux's usual page size (bytes), used to convert /proc/[pid]/stat's RSS field (pages) to bytes.
# Queried for the running system by :func:`page_size`, assumed for a foreign tree for the same
# reason as the tick rate above.
DEFAULT_PAGE_SIZE = 4096

_MAX_ANCESTOR_DEPTH = 15

# Files read at once while walking down a process tree (see read_proc_subtree).
_SUBTREE_WORKERS = 16


@dataclass(frozen=True)
class ProcEntry:
    """One process as read from ``/proc/[pid]/stat``.

    Attributes
    ----------
    comm : str
        The process name (stat field 2).  The kernel caps it at 15 characters,
        so a longer executable name arrives truncated - callers matching against
        a name table must allow for that.
    ppid : int
        Parent process id (field 4).
    starttime : int
        Start time in clock ticks since boot (field 22) - the same unit the
        Claude Code session registry records in ``procStart`` on Linux.
    rss_pages : int or None
        Resident set size in pages (field 24), or ``None`` when it did not parse.
    cpu_ticks : int or None
        Cumulative ``utime + stime`` (fields 14 and 15), or ``None`` when either
        did not parse.
    """

    comm: str
    ppid: int
    starttime: int
    rss_pages: int | None
    cpu_ticks: int | None


def clk_tck() -> int:
    """Return the running system's clock ticks per second, or the default when unavailable."""
    try:
        value = os.sysconf('SC_CLK_TCK')
    except (AttributeError, ValueError, OSError):
        return DEFAULT_CLK_TCK

    return int(value) if value and value > 0 else DEFAULT_CLK_TCK


def page_size() -> int:
    """Return the running system's page size in bytes, or the default when unavailable."""
    try:
        value = os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, ValueError, OSError):
        return DEFAULT_PAGE_SIZE

    return int(value) if value and value > 0 else DEFAULT_PAGE_SIZE


def read_proc_table(proc_dir: Path) -> dict[int, ProcEntry]:
    """Return one :class:`ProcEntry` per numeric entry under *proc_dir*.

    Any ``OSError`` while listing *proc_dir* (the tree unreachable, a share gone) yields an empty
    table rather than raising; likewise a pid whose own ``stat`` file cannot be read, or whose
    ``comm``, ``ppid``, or ``starttime`` do not parse, is skipped rather than aborting the whole
    scan.  ``rss_pages`` and ``cpu_ticks`` feed the resource panel alone, so either degrades to
    ``None`` on its own rather than dropping the whole entry.
    """
    table: dict[int, ProcEntry] = {}

    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return table

    for entry in entries:
        if not entry.name.isdigit():
            continue

        parsed_entry = _read_entry(entry)
        if parsed_entry is not None:
            table[int(entry.name)] = parsed_entry

    return table


def read_proc_subtree(proc_dir: Path, roots: Iterable[tuple[int, int | None]]) -> dict[int, ProcEntry] | None:
    """Return the entries of the *roots* and all their descendants, or None when the kernel lists no children.

    Walks down from each pid through ``task/[tid]/children`` instead of reading every process's
    ``stat``: a WSL distro running containers holds a thousand processes or more, and over the
    ``\\\\wsl.localhost`` share each read costs milliseconds, so the full table took seconds on
    every poll while the sessions' own trees are a few dozen entries.  The result has the shape
    :func:`read_proc_table` returns, restricted to those trees, so :func:`children_index` and
    :func:`live_descendants` answer exactly as they would on the full table.

    Each root is ``(pid, starttime)``, the start time ``None`` when unknown.  The directory listing
    (one cheap read) decides which pids exist, so the registry's many ended sessions cost nothing; a
    pid that exists with another start time was recycled - its entry is kept, so liveness reads it as
    stale, but its unrelated tree is not walked.  A pid whose ``stat`` is gone is simply absent, like
    a vanished process during a full scan.  An unreachable tree yields an empty table.  ``None`` means
    a ``children`` list was missing for a process that exists - a kernel built without
    ``CONFIG_PROC_CHILDREN`` - and the caller falls back to :func:`read_proc_table`.
    """
    table: dict[int, ProcEntry] = {}
    try:
        existing = {name for name in os.listdir(proc_dir) if name.isdigit()}
    except OSError:
        return table

    candidates = [(pid, starttime) for pid, starttime in roots if str(pid) in existing]
    # One generation of the trees at a time, each read in parallel: over the
    # share every file is a round trip, and a node process alone lists a dozen
    # threads whose children have to be read.
    with ThreadPoolExecutor(max_workers=_SUBTREE_WORKERS) as pool:
        level: list[int] = []
        for (pid, starttime), entry in zip(candidates, pool.map(lambda root: _read_entry(proc_dir / str(root[0])), candidates)):
            if entry is None or pid in table:
                continue
            table[pid] = entry
            if starttime is None or entry.starttime == starttime:
                level.append(pid)

        seen: set[int] = set(level)
        while level:
            # Two flat rounds rather than one nested one: a job waiting on jobs
            # of its own pool would block it once every worker did so.
            task_lists = list(pool.map(lambda pid: _list_tasks(proc_dir / str(pid)), level))
            if any(tasks is None for tasks in task_lists):
                return None
            tasks = [task for task_list in task_lists for task in task_list]
            children_by_task = dict(zip(tasks, pool.map(_read_task_children, tasks)))

            next_level: list[int] = []
            for pid, task_list in zip(level, task_lists):
                lists = [children_by_task[task] for task in task_list]
                if task_list and all(children is None for children in lists):
                    if (proc_dir / str(pid)).exists():
                        return None
                    continue
                next_level.extend(child for children in lists if children for child in children if child not in seen)
            seen.update(next_level)
            entries = list(pool.map(lambda pid: _read_entry(proc_dir / str(pid)), next_level))
            level = []
            for pid, entry in zip(next_level, entries):
                if entry is not None:
                    table[pid] = entry
                    level.append(pid)

    return table


def _read_entry(pid_dir: Path) -> ProcEntry | None:
    """Parse one process's ``stat`` file, or None when it is gone or does not parse."""
    try:
        text = (pid_dir / 'stat').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None

    parsed = parse_stat(text)
    if parsed is None:
        return None
    comm, fields = parsed

    try:
        ppid = int(fields[1])
        starttime = int(fields[19])
    except (IndexError, ValueError):
        return None

    rss_pages = _optional_int(fields, 21)
    utime = _optional_int(fields, 11)
    stime = _optional_int(fields, 12)
    cpu_ticks = None if utime is None or stime is None else utime + stime

    return ProcEntry(comm, ppid, starttime, rss_pages, cpu_ticks)


def _list_tasks(pid_dir: Path) -> list[Path] | None:
    """Return the process's thread directories, ``[]`` when the process is gone, or None when it has no ``task`` directory.

    A child belongs to the thread that forked it, so every thread's ``children`` list is read.  A
    process that exists without a ``task`` directory - or, checked by the caller, without a single
    ``children`` file in it - means the kernel does not provide the lists.
    """
    try:
        return [task for task in (pid_dir / 'task').iterdir() if task.name.isdigit()]
    except OSError:
        return None if pid_dir.exists() else []


def _read_task_children(task: Path) -> list[int] | None:
    """Return one thread's child pids, ``[]`` when unreadable, or None when the ``children`` file does not exist."""
    try:
        text = (task / 'children').read_text(encoding='ascii', errors='ignore')
    except FileNotFoundError:
        return None
    except OSError:
        return []

    return [int(value) for value in text.split() if value.isdigit()]


def parse_stat(text: str) -> tuple[str, list[str]] | None:
    """Parse one ``/proc/[pid]/stat`` line into ``(comm, fields)``.

    ``comm`` (field 2) is process-settable (e.g. via ``prctl``/``/proc/self/comm``) and may itself
    contain spaces or parentheses - e.g. ``tmux: server (x)`` - so it cannot be delimited by the
    first ``)``.  Every field after it is numeric or a single letter, so the *last* ``)`` in the line
    is always the real close; everything between the first ``' ('`` and that closing paren is
    ``comm``, and everything after it, split on whitespace, is *fields*, where ``fields[N - 3]``
    holds stat field ``N`` (fields 1-2, pid and comm, are already consumed, so field 3, the state,
    lands at ``fields[0]``).  Returns ``None`` when the line has no ``)`` at all, or nothing shaped
    like ``' ('`` before it - either way too malformed to trust.
    """
    head, _, tail = text.rpartition(')')
    start = head.find(' (')
    if start == -1:
        return None

    comm = head[start + 2:]
    return comm, tail.split()


def children_index(table: dict[int, ProcEntry]) -> dict[int, list[int]]:
    """Build ``{parent_pid: [child_pid, ...]}`` from a parsed procfs table."""
    index: dict[int, list[int]] = {}
    for pid, entry in table.items():
        index.setdefault(entry.ppid, []).append(pid)

    return index


def ancestors(pid: int, table: dict[int, ProcEntry]) -> list[tuple[int, str]]:
    """Return *pid*'s ancestor chain as ``(pid, comm)``, nearest first.

    A parent that started after its child cannot be the real parent (the pid was recycled), so the
    walk stops there.  The chain is capped at a fixed depth, and a repeated pid ends it, so a
    corrupted table can never loop.
    """
    chain: list[tuple[int, str]] = []
    visited = {pid}
    current = pid

    for _ in range(_MAX_ANCESTOR_DEPTH):
        entry = table.get(current)
        if entry is None:
            break

        parent = table.get(entry.ppid)
        if parent is None or entry.ppid in visited:
            break

        if parent.starttime > entry.starttime:
            break

        chain.append((entry.ppid, parent.comm))
        visited.add(entry.ppid)
        current = entry.ppid

    return chain


def live_descendants(
    pid: int,
    proc_start_ticks: int | None,
    table: dict[int, ProcEntry],
    index: dict[int, list[int]],
    ticks_per_second: int = DEFAULT_CLK_TCK,
) -> list[tuple[int, str]] | None:
    """Return *pid*'s meaningful descendants, or ``None`` when the session is not alive.

    A pid absent from *table*, or one whose recorded ``starttime`` does not match
    *proc_start_ticks* (the kernel recycled the pid for an unrelated process), is not alive.  The
    comparison only runs when *proc_start_ticks* is not ``None``: ``0`` is a legitimate stat-field
    value (ticks since boot, not since some epoch), never a sentinel for "unknown".

    A descendant that started within :data:`~agent_monitor_for_claude.procinfo.SESSION_HELPER_WINDOW_SECONDS`
    of the session (a stdio MCP server, a watcher started alongside it) is excluded from the result;
    the walk continues *through* it regardless, so a genuine tool child spawned later by that helper
    still counts.

    Unlike the Windows walk, no per-link start-time check is needed: Linux reparents an orphan onto
    init or the nearest subreaper, so a recorded parent is always a real one - where Windows leaves
    the dead parent's pid in place, ready to graft a whole system subtree onto the session once that
    pid is recycled.  The helper window covers what is left of that case anyway, since anything that
    started before the session is excluded by the same comparison.
    """
    entry = table.get(pid)
    if entry is None:
        return None

    if proc_start_ticks is not None and proc_start_ticks != entry.starttime:
        return None

    descendants: list[tuple[int, str]] = []
    visited = {pid}
    pending = list(index.get(pid, []))
    helper_window_ticks = SESSION_HELPER_WINDOW_SECONDS * ticks_per_second

    while pending:
        child_pid = pending.pop()
        if child_pid in visited:
            continue
        visited.add(child_pid)

        child = table.get(child_pid)
        if child is None:
            continue

        if child.starttime - entry.starttime > helper_window_ticks:
            descendants.append((child_pid, child.comm))

        pending.extend(index.get(child_pid, []))

    return descendants


def read_btime(proc_dir: Path) -> int | None:
    """Return the system boot time (epoch seconds) from the ``btime`` line of ``<proc_dir>/stat``.

    A missing or unreadable file, or one with no parseable ``btime`` line, yields ``None`` rather
    than raising - callers degrade the uptime figure instead of failing the whole probe.
    """
    try:
        text = (proc_dir / 'stat').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None

    for line in text.splitlines():
        if line.startswith('btime '):
            return _optional_int(line.split(), 1)

    return None


# Live CPU-tick baselines kept between calls so a percentage can be reported as the delta since the
# previous sample.  Keyed by (origin, pid) rather than pid alone - the running system and each WSL
# distro reuse the same pid range independently, so the origin disambiguates them the same way it
# disambiguates every other per-root lookup in this application.
_sample_lock = threading.Lock()
_sample_cache: dict[tuple[str, int], tuple[int, int, float]] = {}


def sample_cpu(
    origin: str, pid: int, starttime: int, cpu_ticks: int | None, now: float, ticks_per_second: int = DEFAULT_CLK_TCK,
) -> float | None:
    """Return one process's CPU percent, sampled against the previous call for the same ``(origin, pid)``.

    ``cpu_ticks`` is the process's cumulative ``utime + stime`` at *now* - procfs has no
    instantaneous CPU figure, only this running total, so a percentage needs two readings to diff.
    The first sighting of a given ``(origin, pid, starttime)`` therefore has nothing to diff against
    and reads ``None``; a later call within the same process's lifetime computes the ticks elapsed
    over the wall time elapsed since the previous sample.  A cached entry whose ``starttime`` no
    longer matches means the pid was recycled, so it is treated as an unseen first sighting rather
    than diffed against the old process's ticks.  ``cpu_ticks`` itself being ``None`` (the stat
    fields failed to parse) reads as ``None`` and evicts any cached baseline, so a later successful
    read starts over cleanly rather than diffing across the unreadable gap.
    """
    key = (origin, pid)
    if cpu_ticks is None:
        with _sample_lock:
            _sample_cache.pop(key, None)
        return None

    with _sample_lock:
        cached = _sample_cache.get(key)
        _sample_cache[key] = (starttime, cpu_ticks, now)

    if cached is None or cached[0] != starttime:
        return None

    _previous_starttime, previous_ticks, previous_wall = cached
    elapsed = now - previous_wall
    if elapsed <= 0:
        return None

    return max(0.0, ((cpu_ticks - previous_ticks) / ticks_per_second) / elapsed * 100.0)


def prune_sample_cache(origin: str, live_pids: set[int]) -> None:
    """Drop cached CPU baselines for *origin* whose pid fell out of the current descendant set.

    Scoped to *origin* alone: two roots' process panels sample independently, so a pid missing from
    *live_pids* here - simply because this call is for a different origin - must never evict that
    other origin's cached baseline.
    """
    with _sample_lock:
        for key in list(_sample_cache):
            if key[0] == origin and key[1] not in live_pids:
                _sample_cache.pop(key, None)


def _optional_int(fields: list[str], index: int) -> int | None:
    """Parse ``fields[index]`` to an int, or ``None`` when out of range or not numeric."""
    try:
        return int(fields[index])
    except (IndexError, ValueError):
        return None
