"""
Snapshot
========

Assembles the raw session data the UI consumes.  This module reads the local
sources (session registry, transcripts, processes, subagents) and returns a
flat list of raw per-session records.  It performs no status classification,
label formatting, grouping or sorting - all of that derivation lives in the UI
(``agent_monitor_for_claude/ui/logic.js``).  Python's role is purely to provide
data and to keep conversation content out of it.

Every session root (the native Windows install, plus one per running WSL
distro - see ``roots.session_roots``) is assembled here: each record carries
its root's ``origin``/``origin_label`` through untouched, and liveness is
probed per root kind - ``process_probe.probe_all`` for the Windows root,
``wsl.probe_wsl_sessions`` for a WSL root - so a pid from one root is never
looked up against another root's process table.

Everything returned is JSON-serializable and free of conversation content.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .last_seen import note_alive, prune_last_seen, seconds_since_alive
from .paths import SessionRoot, transcript_path
from .process_probe import ProcessInfo, probe_all
from .roots import session_roots
from .sessions import list_sessions
from .settings import ENDED_MAX_AGE, INCLUDE_COMPLETED
from .subagents import count_subagents
from .transcript import prune_scan_cache, state_for
from .wsl import probe_wsl_sessions

__all__ = ['build_snapshot', 'live_or_recent_ids', 'registry_fingerprint']


def build_snapshot() -> dict[str, Any]:
    """Return the raw session overview as a flat list of per-session records, across every session root."""
    sessions: list[dict[str, Any]] = []

    pairs = _collect_pairs()
    probe_map = _probe_map(pairs)

    for root, record in pairs:
        try:
            session = _build_session_record(root, record, probe_map)
        except Exception:
            # Last-resort per-record isolation: the individual readers already
            # degrade gracefully, but an unforeseen failure on one record must
            # skip that record, never blank the entire overview.
            continue

        if session is not None:
            sessions.append(session)

    # Evict scan-cache entries for sessions no longer in the registry so the
    # cache does not grow unbounded over a long-running monitor.
    prune_scan_cache((root, record['session_id'], record['cwd']) for root, record in pairs)
    prune_last_seen((record['origin'], record['session_id']) for _, record in pairs)

    return {
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'sessions': sessions,
    }


def _build_session_record(
    root: SessionRoot, record: dict[str, Any], probe_map: dict[tuple[str, int], ProcessInfo],
) -> dict[str, Any] | None:
    """Assemble one raw session record, or None when an ended session is dropped.

    A record whose root failed to probe (see ``_probe_map``) has no entry in
    *probe_map*; the resulting ``KeyError`` is caught by ``build_snapshot``'s
    own per-record guard, which drops just this record - the same outcome as
    a session whose process turned out not alive.
    """
    info = probe_map[(record['origin'], record['pid'])]
    transcript_state = state_for(root, record['session_id'], record['cwd'])

    if info.alive:
        note_alive(record['origin'], record['session_id'])
    else:
        # A process that ended long ago has nothing worth showing; drop it here
        # so the UI never has to know about the retention policy.
        ended_seconds = seconds_since_alive(record['origin'], record['session_id'])
        if not _include_ended(transcript_state.age_seconds, ended_seconds):
            return None

    subagents = count_subagents(root, record['session_id'], record['cwd'])

    return {
        'pid': record['pid'],
        'session_id': record['session_id'],
        'cwd': record['cwd'],
        'short_name': record['name'],
        'kind': record['kind'],
        'entrypoint': record.get('entrypoint'),
        'native_status': record['native_status'],
        'waiting_for': record['waiting_for'],
        'origin': record['origin'],
        'origin_label': record['origin_label'],
        'alive': info.alive,
        'child_count': info.child_count,
        'host': info.host,
        'via_cli': info.via_cli,
        'has_transcript': transcript_state.has_transcript,
        'has_activity': transcript_state.last_timestamp is not None,
        'last_entry_kind': transcript_state.last_entry_kind,
        'last_stop_reason': transcript_state.last_stop_reason,
        'usage_limited': transcript_state.usage_limited,
        'pending_tool': transcript_state.pending_tool,
        'last_tool_name': transcript_state.last_tool_name,
        'permission_mode': transcript_state.permission_mode,
        'model_id': transcript_state.model,
        'cli_version': transcript_state.cli_version,
        'usage': transcript_state.usage or {},
        'usage_by_model': transcript_state.usage_by_model or {},
        'model_timeline': transcript_state.model_timeline or [],
        'cli_timeline': transcript_state.cli_timeline or [],
        'title': transcript_state.title,
        'subagents_running': subagents.running,
        'subagents_done': subagents.recent_done,
        'subagents_labels': list(subagents.labels),
        'workflows': [
            {'run_id': workflow.run_id, 'total': workflow.total, 'done': workflow.done, 'active': workflow.active}
            for workflow in subagents.workflows
        ],
        'age_seconds': _display_age(transcript_state.age_seconds, record['started_at']),
    }


def live_or_recent_ids() -> set[str]:
    """Return the session ids the live snapshot currently retains, across every session root.

    A session is retained when its process is alive, or when it ended recently
    enough to still be shown - the same liveness-and-retention rule
    ``build_snapshot`` applies (see ``_include_ended``).  History dedupes against
    exactly this set, so a session shows in exactly one view: one the live
    overview drops (dead and older than the retention window) is left for the
    history listing instead of vanishing from both because a stale, un-pruned
    registry record still names it.
    """
    pairs = _collect_pairs()
    probe_map = _probe_map(pairs)

    ids: set[str] = set()
    for root, record in pairs:
        info = probe_map.get((record['origin'], record['pid']))
        if info is not None and info.alive:
            note_alive(record['origin'], record['session_id'])
            ids.add(record['session_id'])
            continue

        age = state_for(root, record['session_id'], record['cwd']).age_seconds
        if _include_ended(age, seconds_since_alive(record['origin'], record['session_id'])):
            ids.add(record['session_id'])

    return ids


def registry_fingerprint() -> str:
    """Return a cheap change fingerprint of the session registry and transcripts, across every session root.

    Built from registry records (origin, pid, session, native status) and each
    transcript's mtime and size - a handful of ``stat()`` calls, no transcript
    parsing and no process probing.  The UI polls this every second and only
    requests a full snapshot when the fingerprint changes, which keeps idle
    cost minimal while reacting to real changes within about a second.  Each
    part is prefixed with its root's ``origin`` so the same pid or session id
    reused across two roots (a Windows process and an unrelated WSL one) never
    collapses two different parts into one, and roots are visited in
    ``session_roots()`` order (Windows first, WSL distros sorted) for a
    fingerprint that is stable across polls when nothing changed.
    """
    parts: list[str] = []
    for root, record in _collect_pairs():
        transcript = transcript_path(root, record['session_id'], record['cwd'])
        try:
            stat_result = transcript.stat()
            transcript_mark = f'{stat_result.st_mtime_ns}:{stat_result.st_size}'
        except OSError:
            transcript_mark = '-'
        parts.append(
            f"{root.origin}:{record['pid']}:{record['session_id']}:{record['native_status']}:"
            f"{record['waiting_for']}:{transcript_mark}"
        )

    return '|'.join(parts)


def _collect_pairs() -> list[tuple[SessionRoot, dict[str, Any]]]:
    """Return (root, record) for every session across every currently available root.

    A root whose registry listing raises an unexpected error is skipped
    entirely - its sessions are simply absent from this poll, never blanking
    the other roots' sessions (the same last-resort isolation ``build_snapshot``
    applies per record, one level up).
    """
    pairs: list[tuple[SessionRoot, dict[str, Any]]] = []
    for root in session_roots():
        try:
            records = list_sessions(root)
        except Exception:
            continue

        pairs.extend((root, record) for record in records)

    return pairs


def _probe_map(pairs: list[tuple[SessionRoot, dict[str, Any]]]) -> dict[tuple[str, int], ProcessInfo]:
    """Probe every session's liveness, one process-table scan per root.

    Windows sessions share one ``probe_all`` scan of the native process table,
    exactly as before WSL support existed; each WSL root gets its own
    ``probe_wsl_sessions`` scan of its own ``/proc``, so a Linux pid is never
    looked up against the Windows table. The result is keyed by
    ``(root.origin, pid)``, so two roots that happen to report the same raw
    pid number - a native Windows process and an unrelated Linux one inside a
    WSL distro - can never collide or read each other's liveness. A root whose
    probe itself raises an unexpected error is skipped entirely: none of its
    sessions get an entry here, so the per-record lookup drops them rather
    than blanking every other root's sessions.
    """
    requests_by_root: dict[SessionRoot, list[tuple[int, int | None]]] = {}
    for root, record in pairs:
        requests_by_root.setdefault(root, []).append((record['pid'], record['proc_start_ticks']))

    probe_map: dict[tuple[str, int], ProcessInfo] = {}
    for root, requests in requests_by_root.items():
        try:
            info_by_pid = probe_all(requests) if root.proc_dir is None else probe_wsl_sessions(root, requests)
        except Exception:
            continue

        for pid, info in info_by_pid.items():
            probe_map[(root.origin, pid)] = info

    return probe_map


def _display_age(transcript_age: float | None, started_at_ms: float | None) -> float | None:
    """Age for display: transcript activity age, else time since the window opened.

    The fallback gives never-used ("new") sessions a meaningful timestamp
    instead of an empty column.
    """
    if transcript_age is not None:
        return transcript_age

    if started_at_ms is None:
        return None

    return max(0.0, time.time() - started_at_ms / 1000)


def _include_ended(age_seconds: float | None, ended_seconds: float | None) -> bool:
    """Return True if an ended session should still be shown.

    The window is measured from the moment the session ended - approximated by
    the last poll that saw its process alive (*ended_seconds*, from
    :mod:`last_seen`) - not from its last transcript entry.  A session that sat
    idle for hours and was then closed has an activity age far past the window
    and would otherwise vanish in the same poll that noticed it ended, which is
    precisely the session someone who closed a window by accident wants back.

    Parameters
    ----------
    age_seconds : float or None
        Age of the transcript's newest entry.  The fallback, used when the
        session was never seen alive by this process (already dead when the
        monitor started), which preserves the previous behaviour there.
    ended_seconds : float or None
        How long ago the session was last seen alive, or None if never seen.
        A sighting can only ever be *newer* than the last entry, so consulting
        it never shortens the window.
    """
    if INCLUDE_COMPLETED:
        return True

    if ended_seconds is not None:
        return ended_seconds < ENDED_MAX_AGE

    return age_seconds is not None and age_seconds < ENDED_MAX_AGE
