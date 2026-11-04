"""
Snapshot
========

Assembles the raw session data the UI consumes.  This module reads the local
sources (session registry, transcripts, processes, subagents) and returns a
flat list of raw per-session records.  It performs no status classification,
label formatting, grouping or sorting - all of that derivation lives in the UI
(``agent_monitor_for_claude/ui/logic.js``).  Python's role is purely to provide
data and to keep conversation content out of it.

Every session root (the install on this machine, plus - on a Windows host - one
per running WSL distro; see ``roots.session_roots``) is assembled here: each
record carries its root's ``origin``/``origin_label`` through untouched, and
liveness is probed per root kind - ``process_probe.probe_all`` for the local
root, whichever system it runs on, ``wsl.probe_wsl_sessions`` for a WSL root -
so a pid from one root is never looked up against another root's process
table.

Everything returned is JSON-serializable and free of conversation content.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .background_scans import completed_background_scans, pause_background_scans
from .last_seen import note_alive, prune_last_seen, seconds_since_alive
from .paths import SessionRoot, transcript_path
from .process_probe import ProcessInfo, probe_all
from .roots import session_roots
from .sessions import list_sessions
from .settings import ENDED_MAX_AGE, INCLUDE_COMPLETED
from .subagents import count_subagents
from .transcript import prune_scan_cache, state_for
from .worktrees import prune_worktree_cache, resolve_worktree
from .wsl import probe_wsl_sessions

__all__ = ['build_snapshot', 'live_or_recent_ids', 'registry_fingerprint']


def build_snapshot() -> dict[str, Any]:
    """Return the raw session overview as a flat list of per-session records, across every session root."""
    with pause_background_scans():
        return _build_snapshot()


def _build_snapshot() -> dict[str, Any]:
    """Assemble :func:`build_snapshot`'s result."""
    sessions: list[dict[str, Any]] = []

    pairs = _collect_pairs()
    probe_map = _probe_map(pairs)

    for root, record in _one_record_per_session(pairs, probe_map):
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
    prune_worktree_cache((root.origin, record['session_id'], record.get('worktree_cwd') or record['cwd']) for root, record in pairs)

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
    if info.alive:
        note_alive(record['origin'], record['session_id'])
    elif _ended_long_ago(root, record):
        return None

    transcript_state = state_for(root, record['session_id'], record['cwd'])

    # A process that ended long ago has nothing worth showing; drop it here so
    # the UI never has to know about the retention policy.
    if not info.alive and not _include_ended(transcript_state.age_seconds, seconds_since_alive(record['origin'], record['session_id'])):
        return None

    subagents = count_subagents(root, record['session_id'], record['cwd'])
    process_age = _seconds_since_ms(record['started_at'])
    api_retry = transcript_state.api_retry

    # Subagents run in-process, so one cannot outlive its session: a "running"
    # agent under a dead process is a phantom the recent window has yet to
    # clear, and its file age must not make an ended session look freshly
    # active.  An agent file last written before the live process started is
    # the same phantom under a new process - the session was reopened after
    # the process that ran the agent ended.  The same reading the UI applies
    # to the running *count* after a force-stopped turn or a reopen.
    subagent_age = subagents.running_age if info.alive and not _older_than_process(subagents.running_age, process_age) else None

    return {
        'pid': record['pid'],
        'session_id': record['session_id'],
        'cwd': record['cwd'],
        'worktree_cwd': record.get('worktree_cwd'),
        'worktree_branch': record.get('worktree_branch'),
        'short_name': record['name'],
        'kind': record['kind'],
        'entrypoint': record.get('entrypoint'),
        'job_id': record.get('job_id'),
        'remote_control': record['remote_control'],
        'native_status': record['native_status'],
        'native_status_age_seconds': _seconds_since_ms(record.get('native_status_since')),
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
        'api_error_kind': transcript_state.api_error_kind,
        'api_error_status': transcript_state.api_error_status,
        'api_error_detail': transcript_state.api_error_detail,
        'rate_limit_type': transcript_state.rate_limit_type,
        'rate_limit_resets_in_seconds': _seconds_until(transcript_state.rate_limit_resets_at),
        'api_retry_attempt': api_retry.attempt if api_retry else None,
        'api_retry_max': api_retry.max_attempts if api_retry else None,
        'api_retry_offline': bool(api_retry and api_retry.offline),
        'api_retry_detail': api_retry.detail if api_retry else None,
        'wakeup_in_seconds': _seconds_until(transcript_state.wakeup_at),
        'turn_end_signal': transcript_state.turn_end_signal,
        'turn_origin': transcript_state.turn_origin,
        'pending_tool': transcript_state.pending_tool,
        'last_tool_name': transcript_state.last_tool_name,
        'permission_mode': transcript_state.permission_mode,
        'auto_denials_consecutive': transcript_state.auto_denials_consecutive,
        'auto_denials_total': transcript_state.auto_denials_total,
        'model_id': transcript_state.model,
        'cli_version': transcript_state.cli_version,
        'usage': transcript_state.usage or {},
        'usage_by_model': transcript_state.usage_by_model or {},
        'model_timeline': transcript_state.model_timeline or [],
        'cli_timeline': transcript_state.cli_timeline or [],
        'title': transcript_state.title,
        'scan_pending': transcript_state.scan_pending,
        'subagents_running': subagents.running,
        'subagents_done': subagents.recent_done,
        'subagents_labels': list(subagents.labels),
        'workflows': [
            {'run_id': workflow.run_id, 'total': workflow.total, 'done': workflow.done, 'active': workflow.active}
            for workflow in subagents.workflows
        ],
        'process_age_seconds': process_age,
        'age_seconds': _display_age(transcript_state.age_seconds, subagent_age, process_age),
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
    for root, record in _one_record_per_session(pairs, probe_map):
        info = probe_map.get((record['origin'], record['pid']))
        if info is not None and info.alive:
            note_alive(record['origin'], record['session_id'])
            ids.add(record['session_id'])
            continue

        if _ended_long_ago(root, record):
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
    reused across two roots (a local process and an unrelated WSL one) never
    collapses two different parts into one, and roots are visited in
    ``session_roots()`` order (the local root first, WSL distros sorted) for a
    fingerprint that is stable across polls when nothing changed.

    The count of finished background scans leads it: a large transcript's
    first scan completes without the file changing, and the snapshot that
    reported it as pending is outdated from that moment on.
    """
    parts: list[str] = [f'scans:{completed_background_scans()}']
    with pause_background_scans():
        for root, record in _collect_pairs():
            transcript = transcript_path(root, record['session_id'], record['cwd'])
            try:
                stat_result = transcript.stat()
                transcript_mark = f'{stat_result.st_mtime_ns}:{stat_result.st_size}'
            except OSError:
                transcript_mark = '-'
            parts.append(
                f"{root.origin}:{record['pid']}:{record['session_id']}:{record['native_status']}:"
                f"{record['waiting_for']}:{record['remote_control']}:{transcript_mark}"
            )

    return '|'.join(parts)


def _collect_pairs() -> list[tuple[SessionRoot, dict[str, Any]]]:
    """Return (root, record) for every session across every currently available root.

    A root whose registry listing raises an unexpected error is skipped
    entirely - its sessions are simply absent from this poll, never blanking
    the other roots' sessions (the same last-resort isolation ``build_snapshot``
    applies per record, one level up).

    A session working in a git worktree is filed back under the project it was
    launched in here (see :mod:`worktrees`), so every caller - the snapshot,
    the change fingerprint, the history dedupe - finds its files alike.
    """
    pairs: list[tuple[SessionRoot, dict[str, Any]]] = []
    for root in session_roots():
        try:
            records = list_sessions(root)
        except Exception:
            continue

        pairs.extend((root, _resolve_worktree_safely(root, record)) for record in records)

    return pairs


def _resolve_worktree_safely(root: SessionRoot, record: dict[str, Any]) -> dict[str, Any]:
    """Return ``worktrees.resolve_worktree``'s record, or *record* unchanged if the lookup fails unexpectedly.

    The lookup degrades on every failure it anticipates; this is the same
    last-resort isolation the callers apply, so one odd record can at worst
    stay where the registry put it.
    """
    try:
        return resolve_worktree(root, record)
    except Exception:
        return record


def _probe_map(pairs: list[tuple[SessionRoot, dict[str, Any]]]) -> dict[tuple[str, int], ProcessInfo]:
    """Probe every session's liveness, one process-table scan per root.

    Local sessions share one ``probe_all`` scan of the running system's own
    process table; each WSL root gets its own ``probe_wsl_sessions`` scan of its
    own ``/proc``, so a pid inside a distro is never looked up against the
    host's table. Which of the two applies is decided by ``root.proc_dir``,
    which is set only for a root read through a *foreign* procfs. The result is
    keyed by ``(root.origin, pid)``, so two roots that happen to report the same
    raw pid number - a local process and an unrelated one inside a WSL distro -
    can never collide or read each other's liveness. A root whose
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


def _one_record_per_session(
    pairs: list[tuple[SessionRoot, dict[str, Any]]], probe_map: dict[tuple[str, int], ProcessInfo],
) -> list[tuple[SessionRoot, dict[str, Any]]]:
    """Drop the registry records an earlier process of the same session left behind.

    Every process writes its own registry record, and one that is not shut down
    cleanly (a Claude Desktop chat reopened many times, a WSL distro switched
    off) leaves it in place - one conversation then carries a dozen records.
    Retention and age are per session, so a single live sibling would keep all
    of them on screen as identical rows.  Per ``(origin, session_id)`` this
    keeps every live record (two processes can genuinely hold one session at
    once) and, only when none is alive, the dead record with the newest
    ``startedAt``.  The original order is preserved.
    """
    def is_alive(record: dict[str, Any]) -> bool:
        info = probe_map.get((record['origin'], record['pid']))
        return info is not None and info.alive

    alive_sessions: set[tuple[str, str]] = set()
    newest_dead: dict[tuple[str, str], dict[str, Any]] = {}
    for _, record in pairs:
        key = (record['origin'], record['session_id'])
        if is_alive(record):
            alive_sessions.add(key)
            continue

        current = newest_dead.get(key)
        if current is None or (record['started_at'] or 0) > (current['started_at'] or 0):
            newest_dead[key] = record

    kept: list[tuple[SessionRoot, dict[str, Any]]] = []
    for root, record in pairs:
        key = (record['origin'], record['session_id'])
        if is_alive(record) or (key not in alive_sessions and newest_dead.get(key) is record):
            kept.append((root, record))

    return kept


def _display_age(transcript_age: float | None, subagent_age: float | None, process_age: float | None) -> float | None:
    """Age for display: the freshest activity anywhere in the session, else time since the window opened.

    A session that delegates its whole turn to a subagent appends nothing to its
    own transcript while that agent works, so the transcript age alone would
    report it as untouched for as long as the delegation lasts - minutes, while
    it plainly works.  The running subagent's own transcript is the missing
    evidence, so whichever of the two is fresher wins.  Only a *running* agent
    contributes (see ``SubagentInfo.running_age``).

    The ``process_age`` fallback gives never-used ("new") sessions a meaningful
    timestamp instead of an empty column.

    Parameters
    ----------
    transcript_age : float or None
        Seconds since the session transcript's newest conversational turn.
    subagent_age : float or None
        Seconds since the most recently written running subagent transcript.
    process_age : float or None
        Seconds since the registry recorded the process start (see
        ``_seconds_since_ms``), used only when neither age is known.
    """
    ages = [age for age in (transcript_age, subagent_age) if age is not None]
    if ages:
        return min(ages)

    return process_age


def _seconds_since_ms(epoch_ms: float | None) -> float | None:
    """Seconds since a registry timestamp in epoch milliseconds, or None without one.

    The process start (``startedAt``) is shipped as ``process_age_seconds``: the
    UI compares it with the transcript age to tell a turn this process wrote
    from one an earlier process left behind in the same session (a reopened
    session), and it is the fallback age of a never-used session.  The last
    native status change (``statusUpdatedAt``) says how long the session has
    been in that status.  Clamped at 0 against clock skew, like every other
    age here.
    """
    if epoch_ms is None:
        return None

    return max(0.0, time.time() - epoch_ms / 1000)


def _seconds_until(epoch_seconds: float | None) -> float | None:
    """Seconds from now until a transcript deadline in POSIX seconds, negative once it has passed, or None.

    Not clamped: whether a scheduled wakeup or a usage limit's reset still lies
    ahead is exactly what the UI reads from the sign.
    """
    if epoch_seconds is None:
        return None

    return epoch_seconds - time.time()


def _older_than_process(age: float | None, process_age: float | None) -> bool:
    """Return True if something *age* seconds old was last written before the live process started.

    An unknown age on either side is no evidence, so it reads as not older.
    """
    if age is None or process_age is None:
        return False

    return age > process_age


def _ended_long_ago(root: SessionRoot, record: dict[str, Any]) -> bool:
    """Return True if an ended session is certainly past the retention window, judged without reading its transcript.

    The registry keeps the records of ended sessions until Claude Code removes
    them, which a hard shutdown never does - hundreds can pile up, and reading
    each one's transcript only to drop it would cost a snapshot many seconds.
    The transcript's modification time settles it: no entry can be newer than
    the file's last write, so a file older than the window cannot hold an
    entry inside it.  The check is the looser of the two - an in-place
    rewrite can bump the time without a new entry - so a session it lets
    through is still judged exactly by :func:`_include_ended`.
    """
    ended_seconds = seconds_since_alive(record['origin'], record['session_id'])
    if ended_seconds is not None or INCLUDE_COMPLETED:
        return not _include_ended(None, ended_seconds)

    try:
        modified = transcript_path(root, record['session_id'], record['cwd']).stat().st_mtime
    except OSError:
        # No transcript means no age, which the exact rule drops as well.
        return True

    return time.time() - modified >= ENDED_MAX_AGE


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
