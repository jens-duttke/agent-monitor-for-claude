"""
Session Inventory
=================

Reads one session root's registry (``<config_dir>/sessions/*.json``, see
:class:`paths.SessionRoot`) - the same set of records the ``claude agents
--json`` command surfaces - directly from disk.  Reading the files avoids
spawning the CLI on every poll; liveness of each PID is determined separately
via the process probe.

Every returned record is stamped with the root it came from (``origin``,
``origin_label``), so a caller merging records from several roots - the
native Windows install plus any running WSL distro - can always tell which
root each one belongs to.

Parsing is defensive: the registry schema is an unversioned Claude Code
internal, so a record missing its required fields is skipped rather than
raising.

The registry is listed on every snapshot and every change-fingerprint poll,
and records of ended sessions can pile up by the hundred - in a WSL distro,
whose files are read over a slow network share, that would cost seconds per
poll.  Each file's parsed record is therefore cached by its modification time
and size, which the directory listing already supplies, so only a file that
changed is read again.  Nothing is written to disk.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli_install import parse_version
from .paths import SessionRoot, sessions_dir

__all__ = ['list_sessions']

# A file modified this recently is read again even when its time and size are
# unchanged: two rewrites within one timestamp tick can leave both identical
# (``busy`` -> ``idle`` keeps the size), and the second must not be missed.
_RECENT_WRITE_SECONDS = 2.0


@dataclass(frozen=True)
class _CachedRecord:
    """One registry file's normalized record (None if invalid), keyed by its time and size."""

    mtime_ns: int
    size: int
    record: dict[str, Any] | None


# Registry directory -> file name -> its cached record.  Each listing replaces
# its directory's map, which drops the files that have gone.
_registry_cache: dict[str, dict[str, _CachedRecord]] = {}

# The snapshot, the change fingerprint and the history dedupe each list the
# registry, on different pywebview worker threads.
_lock = threading.Lock()

# Registry files read at once on a first listing (see list_sessions).
_READ_WORKERS = 16


def list_sessions(root: SessionRoot) -> list[dict[str, Any]]:
    """Return normalized session records from *root*'s on-disk registry.

    Each record has ``session_id``, ``pid``, ``cwd``, ``name``, ``kind``,
    ``job_id``, ``remote_control``, ``started_at``, ``native_status_since``,
    ``process_version``, ``origin``, and ``origin_label``.  Records that cannot
    be parsed are omitted.

    Parameters
    ----------
    root : SessionRoot
        The session root to read the registry from; also the source of the
        ``origin``/``origin_label`` stamped onto every returned record.
    """
    directory = sessions_dir(root)
    try:
        with os.scandir(directory) as entries:
            files = sorted((entry for entry in entries if entry.name.endswith('.json')), key=lambda entry: entry.name)
    except OSError:
        return []

    with _lock:
        previous = _registry_cache.get(str(directory), {})

    # Read in parallel: on a WSL root every file is a round trip over the share,
    # and a distro's registry keeps hundreds of ended sessions' records, so the
    # first listing took seconds one file at a time. Later ones hit the cache.
    with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        loaded = list(pool.map(lambda entry: _cached_or_read(entry, previous.get(entry.name)), files))

    current: dict[str, _CachedRecord] = {}
    records: list[dict[str, Any]] = []
    for entry, cached in zip(files, loaded):
        if cached is None:
            continue

        current[entry.name] = cached
        if cached.record is None:
            continue

        # A copy, so a caller stamping or rewriting fields never alters the cache.
        record = dict(cached.record)
        record['origin'] = root.origin
        record['origin_label'] = root.label
        records.append(record)

    with _lock:
        _registry_cache[str(directory)] = current

    return records


def _cached_or_read(entry: os.DirEntry[str], cached: _CachedRecord | None) -> _CachedRecord | None:
    """Return *entry*'s cached record if the file is unchanged, else read it; None if it cannot be stat'ed.

    On Windows the directory listing already carries the time and size, so an
    unchanged file costs no file-system call at all.
    """
    try:
        stat_result = entry.stat()
    except OSError:
        return None

    recently_written = time.time() - stat_result.st_mtime < _RECENT_WRITE_SECONDS
    if (cached is not None and not recently_written and cached.mtime_ns == stat_result.st_mtime_ns
            and cached.size == stat_result.st_size):
        return cached

    return _CachedRecord(stat_result.st_mtime_ns, stat_result.st_size, _normalize(_read_json(Path(entry.path))))


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file, or return None on any error."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _normalize(data: Any) -> dict[str, Any] | None:
    """Map a raw registry record to the normalized shape, or None if invalid."""
    if not isinstance(data, dict):
        return None

    session_id = data.get('sessionId')
    pid = data.get('pid')
    cwd = data.get('cwd')

    if (not isinstance(session_id, str) or not session_id or not isinstance(pid, int) or isinstance(pid, bool)
            or not isinstance(cwd, str) or not cwd):
        return None

    name = data.get('name')
    kind = data.get('kind')

    entrypoint = data.get('entrypoint')
    native_status = data.get('status')
    waiting_for = data.get('waitingFor')
    # Only a background session carries one: the short id `claude attach`,
    # `logs` and `stop` take, which is the only handle such a session has - it
    # runs under the daemon with no window to raise.
    job_id = data.get('jobId')
    # Remote Control is called the "bridge" internally: while the session is
    # reachable from claude.ai, the registry names the cloud session it is
    # bridged to.  Only whether one is named is taken - the id itself is a
    # cloud identifier no feature here needs.
    bridge_session_id = data.get('bridgeSessionId')

    return {
        'native_status': native_status if isinstance(native_status, str) else None,
        'waiting_for': waiting_for if isinstance(waiting_for, str) else None,
        'proc_start_ticks': _parse_proc_start(data.get('procStart')),
        'session_id': session_id,
        'pid': pid,
        'cwd': cwd,
        'name': name if isinstance(name, str) and name else session_id[:8],
        'kind': kind if isinstance(kind, str) else 'interactive',
        'entrypoint': entrypoint if isinstance(entrypoint, str) else None,
        'job_id': job_id if isinstance(job_id, str) and job_id else None,
        'remote_control': isinstance(bridge_session_id, str) and bool(bridge_session_id),
        'started_at': _parse_epoch_ms(data.get('startedAt')),
        # When the native status last changed - how long a session has been busy,
        # idle or waiting, which the transcript cannot tell for a long turn.
        'native_status_since': _parse_epoch_ms(data.get('statusUpdatedAt')),
        # The Claude Code version of the running process - unlike the version on
        # the transcript's newest turn, it is known before the first turn and
        # never belongs to an earlier process of a reopened session.
        'process_version': parse_version(data.get('version')),
    }


def _parse_proc_start(value: Any) -> int | None:
    """Parse the ``procStart`` field (a 100 ns tick count as a digit string) to an int."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None

    try:
        ticks = int(value)
    except ValueError:
        return None

    return ticks if ticks > 0 else None


def _parse_epoch_ms(value: Any) -> float | None:
    """Parse a registry timestamp in epoch milliseconds (``startedAt``, ``statusUpdatedAt``) to a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value) if value > 0 else None
