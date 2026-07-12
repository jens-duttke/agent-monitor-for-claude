"""
Snapshot
========

Assembles the raw session data the UI consumes.  This module reads the local
sources (session registry, transcripts, processes, subagents) and returns a
flat list of raw per-session records.  It performs no status classification,
label formatting, grouping or sorting - all of that derivation lives in the UI
(``agent_monitor_for_claude/ui/logic.js``).  Python's role is purely to provide
data and to keep conversation content out of it.

Everything returned is JSON-serializable and free of conversation content.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .paths import transcript_path
from .process_probe import probe_all
from .sessions import list_sessions
from .settings import ENDED_MAX_AGE, INCLUDE_COMPLETED
from .subagents import count_subagents
from .transcript import state_for

__all__ = ['build_snapshot', 'registry_fingerprint']


def build_snapshot() -> dict[str, Any]:
    """Return the raw session overview as a flat list of per-session records."""
    sessions: list[dict[str, Any]] = []

    records = list_sessions()
    probe_map = probe_all([(record['pid'], record['proc_start_ticks']) for record in records])

    for record in records:
        try:
            session = _build_session_record(record, probe_map)
        except Exception:
            # Last-resort per-record isolation: the individual readers already
            # degrade gracefully, but an unforeseen failure on one record must
            # skip that record, never blank the entire overview.
            continue

        if session is not None:
            sessions.append(session)

    return {
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'sessions': sessions,
    }


def _build_session_record(record: dict[str, Any], probe_map: dict[int, Any]) -> dict[str, Any] | None:
    """Assemble one raw session record, or None when an ended session is dropped."""
    info = probe_map[record['pid']]
    transcript_state = state_for(record['session_id'], record['cwd'])

    # A process that ended long ago has nothing worth showing; drop it here
    # so the UI never has to know about the retention policy.
    if not info.alive and not _include_ended(transcript_state.age_seconds):
        return None

    subagents = count_subagents(record['session_id'], record['cwd'])

    return {
        'pid': record['pid'],
        'session_id': record['session_id'],
        'cwd': record['cwd'],
        'short_name': record['name'],
        'kind': record['kind'],
        'entrypoint': record.get('entrypoint'),
        'native_status': record['native_status'],
        'waiting_for': record['waiting_for'],
        'alive': info.alive,
        'child_count': info.child_count,
        'child_names': list(info.child_names),
        'host': info.host,
        'via_cli': info.via_cli,
        'has_transcript': transcript_state.has_transcript,
        'has_activity': transcript_state.last_timestamp is not None,
        'last_entry_kind': transcript_state.last_entry_kind,
        'last_stop_reason': transcript_state.last_stop_reason,
        'pending_tool': transcript_state.pending_tool,
        'last_tool_name': transcript_state.last_tool_name,
        'permission_mode': transcript_state.permission_mode,
        'model_id': transcript_state.model,
        'usage': transcript_state.usage or {},
        'usage_by_model': transcript_state.usage_by_model or {},
        'model_timeline': transcript_state.model_timeline or [],
        'title': transcript_state.title,
        'subagents_running': subagents.running,
        'subagents_done': subagents.recent_done,
        'subagents_labels': list(subagents.labels),
        'age_seconds': _display_age(transcript_state.age_seconds, record['started_at']),
    }


def registry_fingerprint() -> str:
    """Return a cheap change fingerprint of the session registry and transcripts.

    Built from registry records (pid, session, native status) and each
    transcript's mtime and size - a handful of ``stat()`` calls, no transcript
    parsing and no process probing.  The UI polls this every second and only
    requests a full snapshot when the fingerprint changes, which keeps idle
    cost minimal while reacting to real changes within about a second.
    """
    parts: list[str] = []
    for record in list_sessions():
        transcript = transcript_path(record['session_id'], record['cwd'])
        try:
            stat_result = transcript.stat()
            transcript_mark = f'{stat_result.st_mtime_ns}:{stat_result.st_size}'
        except OSError:
            transcript_mark = '-'
        parts.append(f"{record['pid']}:{record['session_id']}:{record['native_status']}:{record['waiting_for']}:{transcript_mark}")

    return '|'.join(parts)


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


def _include_ended(age_seconds: float | None) -> bool:
    """Return True if an ended session should still be shown."""
    if INCLUDE_COMPLETED:
        return True

    return age_seconds is not None and age_seconds < ENDED_MAX_AGE
