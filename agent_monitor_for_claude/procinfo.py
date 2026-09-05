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

__all__ = ['ChildProcessStat', 'ProcessInfo', 'SESSION_HELPER_WINDOW_SECONDS']

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
class ProcessInfo:
    """Result of probing a session's process."""

    alive: bool
    tool_running: bool
    host: str | None = None
    via_cli: bool = False
    child_count: int = 0


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
