"""
Linux Process Backend
======================

Linux implementation of the process-introspection API; see
:mod:`agent_monitor_for_claude.process_probe` for the dispatch and for what
each function promises.  Everything is derived from one scan of ``/proc``
(:mod:`agent_monitor_for_claude.procfs`), which reads only ``/proc/[pid]/stat``
- process names, parent links, start times, and resource counters, never a
command line.

The session registry's ``procStart`` means something different here than it
does on Windows: on Linux Claude Code records exactly field 22 of
``/proc/[pid]/stat``, the process start time in clock ticks since boot.  It is
therefore compared against that field directly - a mismatch means the kernel
recycled the pid for an unrelated process and the registry entry is stale, so
the session is reported as not alive.

Names come from ``stat``'s ``comm`` field, which the kernel caps at 15
characters.  The host tables below are written with full executable names and
:func:`_with_truncations` registers each long name's 15-character prefix
alongside it, so ``gnome-terminal-server`` is still recognised when it arrives
as ``gnome-terminal-``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from .. import procfs
from ..procinfo import ChildProcessStat, ProcessInfo

__all__ = [
    'IGNORED_ANCESTOR_NAMES', 'TERMINAL_WINDOW_OWNERS',
    'ancestry', 'probe_all', 'process_names', 'process_stats', 'vmmem_present',
]

# The running system's own process tree.
_PROC_DIR = Path('/proc')

# Origin scoping the shared CPU-sample cache; the local root's own origin, so a
# WSL distro read from a Windows host can never share or evict these baselines.
_ORIGIN = 'local'

# The kernel truncates a process name to this many characters in ``comm``.
_COMM_MAX = 15


def _with_truncations(names: dict[str, str]) -> dict[str, str]:
    """Return *names* plus an entry for every key the kernel would truncate.

    A name longer than :data:`_COMM_MAX` never reaches us in full, so each such
    key is registered under its 15-character prefix as well.  The full key is
    kept too: it costs nothing and keeps the table readable as a list of real
    executable names.
    """
    expanded = dict(names)
    for name, label in names.items():
        if len(name) > _COMM_MAX:
            expanded.setdefault(name[:_COMM_MAX], label)

    return expanded


# Editors that host a session in their own window.  A JetBrains IDE started
# through its shell launcher runs as ``java`` and cannot be told apart from any
# other JVM by name alone, so only the IDEs that keep their own binary name
# (native launchers, snaps, AppImages) are recognised here.
_EDITOR_HOSTS = _with_truncations({
    'code': 'VS Code',
    'code-insiders': 'VS Code Insiders',
    'codium': 'VSCodium',
    'cursor': 'Cursor',
    'windsurf': 'Windsurf',
    'pycharm': 'PyCharm',
    'idea': 'IntelliJ IDEA',
    'webstorm': 'WebStorm',
    'phpstorm': 'PhpStorm',
    'rider': 'Rider',
    'clion': 'CLion',
    'goland': 'GoLand',
    'rubymine': 'RubyMine',
    'datagrip': 'DataGrip',
})

# Terminal emulators that host a session in their own window.
_TERMINAL_HOSTS = _with_truncations({
    'gnome-terminal-server': 'GNOME Terminal',
    'ptyxis': 'Ptyxis',
    'ptyxis-agent': 'Ptyxis',
    'konsole': 'Konsole',
    'xfce4-terminal': 'Xfce Terminal',
    'mate-terminal': 'MATE Terminal',
    'lxterminal': 'LXTerminal',
    'qterminal': 'QTerminal',
    'deepin-terminal': 'Deepin Terminal',
    'terminator': 'Terminator',
    'tilix': 'Tilix',
    'guake': 'Guake',
    'sakura': 'Sakura',
    'terminology': 'Terminology',
    'blackbox': 'Black Box',
    'alacritty': 'Alacritty',
    'kitty': 'kitty',
    'wezterm-gui': 'WezTerm',
    'foot': 'foot',
    'contour': 'Contour',
    'hyper': 'Hyper',
    'tabby': 'Tabby',
    'warp-terminal': 'Warp',
    'xterm': 'XTerm',
    'urxvt': 'rxvt-unicode',
    'st': 'st',
})

# GUI hosts (editors and terminal emulators).
_GUI_HOSTS = {**_EDITOR_HOSTS, **_TERMINAL_HOSTS}

# Unlike Windows, a Linux terminal emulator keeps the session on its own process
# chain, so the ancestor walk finds its window without help.  The title-matching
# fallback is still offered for the cases the chain cannot cover (a session
# reattached to a different terminal), and it is confined to these owners.
TERMINAL_WINDOW_OWNERS = frozenset(_TERMINAL_HOSTS)

# Ancestors that own windows for the whole desktop, never for one session; the
# window search skips them so it never raises the shell instead of the editor.
# Lowercase throughout: ``ancestry`` lowercases every name it reports, so a
# capitalised entry here (``Xorg`` as the binary spells itself) could never match.
IGNORED_ANCESTOR_NAMES = frozenset({
    'systemd', 'init', 'gnome-shell', 'plasmashell', 'xfce4-session', 'mate-session',
    'cinnamon', 'lxsession', 'kwin_wayland', 'kwin_x11', 'mutter', 'xorg',
})

# Shells; one appearing between the session process and its GUI host means the
# session is driven through the CLI rather than the editor extension.
_SHELL_HOSTS = _with_truncations({
    'bash': 'Bash',
    'sh': 'Shell',
    'dash': 'Shell',
    'zsh': 'Zsh',
    'fish': 'fish',
    'ksh': 'ksh',
    'tcsh': 'tcsh',
    'csh': 'csh',
    'nu': 'Nushell',
    'xonsh': 'xonsh',
    'elvish': 'Elvish',
})


def probe_all(requests: Iterable[tuple[int, int | None]]) -> dict[int, ProcessInfo]:
    """Probe many sessions from one ``/proc`` scan.

    Parameters
    ----------
    requests : iterable of (pid, proc_start_ticks)
        Process IDs from the session registry, each with the recorded process
        start time (``/proc/[pid]/stat`` field 22) or ``None``.

    Returns
    -------
    dict[int, ProcessInfo]
        One entry per requested PID.  An unreadable ``/proc`` degrades every
        request to not alive rather than raising.
    """
    table = procfs.read_proc_table(_PROC_DIR)
    index = procfs.children_index(table)
    ticks_per_second = procfs.clk_tck()

    result: dict[int, ProcessInfo] = {}
    for pid, proc_start_ticks in requests:
        descendants = procfs.live_descendants(pid, proc_start_ticks, table, index, ticks_per_second)
        if descendants is None:
            result[pid] = ProcessInfo(alive=False, tool_running=False)
            continue

        host, via_cli = _classify_ancestry([name.lower() for _pid, name in procfs.ancestors(pid, table)])
        result[pid] = ProcessInfo(
            alive=True,
            tool_running=bool(descendants),
            host=host,
            via_cli=via_cli,
            child_count=len(descendants),
        )

    return result


def ancestry(pid: int) -> list[tuple[int, str]]:
    """Return the live ancestor chain of *pid* as ``(pid, name_lower)``, nearest first."""
    table = procfs.read_proc_table(_PROC_DIR)
    return [(ancestor_pid, name.lower()) for ancestor_pid, name in procfs.ancestors(pid, table)]


def process_names() -> dict[int, str]:
    """Return ``{pid: lowercased process name}`` for every running process."""
    return {pid: entry.comm.lower() for pid, entry in procfs.read_proc_table(_PROC_DIR).items()}


def vmmem_present() -> bool:
    """Return False - the WSL2 utility VM is a Windows-host concept with no Linux counterpart.

    WSL discovery is gated on this, so a Linux host never reaches any of it.
    """
    return False


def process_stats(pid: int, proc_start_ticks: int | None = None) -> list[ChildProcessStat]:
    """Return live CPU / memory / uptime for a session's descendant processes.

    The descendant set and the liveness gate are exactly :func:`probe_all`'s - both go through
    ``procfs.live_descendants`` - so the panel lists precisely the processes the badge counts, and a
    dead or stale session yields ``[]``.  Memory and uptime come straight from the scan:
    ``rss_bytes`` is the resident-pages field times the page size, and ``uptime_seconds`` is *now*
    minus the process's absolute start time (the system boot time plus its own ``starttime``).
    ``/proc`` has no instantaneous CPU figure, only cumulative ticks, so CPU is sampled against the
    previous call: the first reading of a freshly seen process is ``None`` and a real percentage
    follows a second later.

    Parameters
    ----------
    pid : int
        The session process id from the registry.
    proc_start_ticks : int or None
        The recorded process start time (``/proc/[pid]/stat`` field 22); when given, a mismatch
        means the pid was recycled and an empty list is returned.

    Returns
    -------
    list[ChildProcessStat]
        One entry per descendant process, ordered by name then pid so the rows stay put across
        refreshes.  Empty when the session process is gone or stale.
    """
    table = procfs.read_proc_table(_PROC_DIR)
    index = procfs.children_index(table)
    ticks_per_second = procfs.clk_tck()

    descendants = procfs.live_descendants(pid, proc_start_ticks, table, index, ticks_per_second)
    if descendants is None:
        return []

    now = time.time()
    btime = procfs.read_btime(_PROC_DIR)
    bytes_per_page = procfs.page_size()

    stats: list[ChildProcessStat] = []
    live_pids: set[int] = set()
    for child_pid, name in descendants:
        entry = table.get(child_pid)
        if entry is None:
            continue

        live_pids.add(child_pid)
        rss_bytes = None if entry.rss_pages is None else entry.rss_pages * bytes_per_page
        uptime = None if btime is None else max(0.0, now - (btime + entry.starttime / ticks_per_second))
        cpu = procfs.sample_cpu(_ORIGIN, child_pid, entry.starttime, entry.cpu_ticks, now, ticks_per_second)
        stats.append(ChildProcessStat(pid=child_pid, name=name, cpu_percent=cpu, rss_bytes=rss_bytes, uptime_seconds=uptime))

    stats.sort(key=lambda stat: (stat.name, stat.pid))
    procfs.prune_sample_cache(_ORIGIN, live_pids)

    return stats


def _classify_ancestry(ancestor_names: list[str]) -> tuple[str | None, bool]:
    """Derive ``(host, via_cli)`` from ancestor names ordered nearest first.

    The first GUI host on the chain labels where the session runs right now; a shell encountered
    before it means the session is driven through the CLI.  Without any GUI host, the nearest shell
    itself is the host.
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
