"""
Process Introspection Types
============================

The platform-neutral result types the process probe returns, plus the one
timing constant every backend shares.  They live here rather than in
:mod:`agent_monitor_for_claude.process_probe` so that both the per-platform
backends under ``platforms/`` and the WSL probe can import them without
importing the dispatcher that imports *them*.

Nothing in this module touches the operating system - it is data shapes only.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ['ChildProcess', 'ChildProcessStat', 'ProcessInfo', 'SESSION_HELPER_WINDOW_SECONDS', 'child_processes']

# Background processes listed per session; past this many the rest still count.
_MAX_CHILD_PROCESSES = 200
# How far up the tree a listed process looks for the nearest listed ancestor.
_MAX_PARENT_HOPS = 32

# A child that starts together with the session process is a session-lifetime
# helper (a stdio MCP server, a file watcher), not a tool execution: a tool
# child is spawned later, when the tool actually runs.  Descendants that start
# within this window of the session's own start are therefore not counted as
# running tools - otherwise a configured stdio MCP server (node/python/docker,
# common) would make every session read as busy and hide "needs you" prompts
# for its entire lifetime.  The window only affects the first seconds of a
# session's life; a tool run later reads normally, and the transcript-based
# classification still reports a just-started turn as working regardless.
SESSION_HELPER_WINDOW_SECONDS = 10.0


@dataclass(frozen=True)
class ChildProcess:
    """One background process of a session: its pid, its name, and the nearest listed process above it (0: the session itself)."""

    pid: int
    parent: int
    name: str


@dataclass(frozen=True)
class ProcessInfo:
    """Result of probing a session's process."""

    alive: bool
    tool_running: bool
    host: str | None = None
    via_cli: bool = False
    child_count: int = 0
    children: tuple[ChildProcess, ...] = ()


def child_processes(session_pid: int, descendants: list[tuple[int, str]], parent_of: dict[int, int]) -> tuple[ChildProcess, ...]:
    """The session's counted descendants as a tree: each under the nearest of them above it, else under the session.

    *descendants* is the walk's ``(pid, name)`` list and *parent_of* each
    process's parent pid from the same table.  A process whose parent is not
    listed - a console host, a helper started with the session - hangs off the
    first listed process further up, so the tree keeps its shape.
    """
    listed = {pid for pid, _name in descendants}
    result: list[ChildProcess] = []
    for pid, name in descendants[:_MAX_CHILD_PROCESSES]:
        parent = parent_of.get(pid, session_pid)
        for _ in range(_MAX_PARENT_HOPS):
            if parent in listed or parent == session_pid or parent not in parent_of:
                break
            parent = parent_of[parent]
        result.append(ChildProcess(pid, parent if parent in listed else 0, name))

    return tuple(sorted(result, key=lambda child: child.pid))


@dataclass(frozen=True)
class ChildProcessStat:
    """Live resource usage of one process shown in the process panel.

    ``kind`` is ``'process'`` for a real descendant process, or ``'wsl_vm'`` for
    the shared WSL2 utility VM appended as context (see
    ``platforms.process_win32.process_stats``); a VM row's figures are
    machine-wide, not this session's, so the UI labels it.
    """

    pid: int
    name: str
    cpu_percent: float | None
    rss_bytes: int | None
    uptime_seconds: float | None
    kind: str = 'process'
