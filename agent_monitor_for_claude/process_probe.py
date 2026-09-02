"""
Process Probe
=============

Isolates the process-introspection side effects and selects the backend for
the running system: :mod:`agent_monitor_for_claude.platforms.process_win32`
reads the Windows process table, :mod:`~agent_monitor_for_claude.platforms.process_linux`
reads ``/proc``.  Both answer the same questions and return the same shapes, so
nothing above this module branches on the operating system.

A single scan of the process table per snapshot answers, for every session at
once: is the process alive, is a tool currently executing (meaningful child
process), which application hosts it right now, and is it driven through the
CLI (a shell sits between the session process and its GUI host).  Working from
one scan keeps the result current on every poll and avoids per-PID process
walks.  Only process names, parent links, and start times are inspected -
never command lines or arguments.

``process_stats`` is a separate, on-demand path: it reports live CPU, memory
(RSS), and uptime for one session's descendant processes, and is used only
while the user has the process panel open.  It inspects a handful of processes
rather than the whole table, which is why it stays out of the per-second
snapshot scan above.  It still reads no command line or argument.

Both backends validate the session registry's recorded process start time
(``procStart``) against the live process: a mismatch means the system recycled
the PID for an unrelated process and the registry entry is stale, so the
session is reported as not alive.  What that field *contains* differs per
system - a 100 ns tick count on Windows, clock ticks since boot on Linux - and
each backend reads it its own way.
"""
from __future__ import annotations

import sys

from .procinfo import ChildProcessStat, ProcessInfo, SESSION_HELPER_WINDOW_SECONDS

if sys.platform == 'win32':
    from .platforms.process_win32 import (
        IGNORED_ANCESTOR_NAMES, TERMINAL_WINDOW_OWNERS, ancestry, probe_all, process_names, process_stats, vmmem_present,
    )
else:
    from .platforms.process_linux import (
        IGNORED_ANCESTOR_NAMES, TERMINAL_WINDOW_OWNERS, ancestry, probe_all, process_names, process_stats, vmmem_present,
    )

__all__ = [
    'ChildProcessStat', 'IGNORED_ANCESTOR_NAMES', 'ProcessInfo', 'SESSION_HELPER_WINDOW_SECONDS',
    'TERMINAL_WINDOW_OWNERS', 'ancestry', 'probe', 'probe_all', 'process_names', 'process_stats', 'vmmem_present',
]


def probe(pid: int, proc_start_ticks: int | None = None) -> ProcessInfo:
    """Probe a single session process (convenience wrapper around ``probe_all``)."""
    return probe_all([(pid, proc_start_ticks)])[pid]
