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
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import SessionRoot, sessions_dir

__all__ = ['list_sessions']


def list_sessions(root: SessionRoot) -> list[dict[str, Any]]:
    """Return normalized session records from *root*'s on-disk registry.

    Each record has ``session_id``, ``pid``, ``cwd``, ``name``, ``kind``,
    ``started_at``, ``origin``, and ``origin_label``.  Records that cannot be
    parsed are omitted.

    Parameters
    ----------
    root : SessionRoot
        The session root to read the registry from; also the source of the
        ``origin``/``origin_label`` stamped onto every returned record.
    """
    directory = sessions_dir(root)
    if not directory.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob('*.json')):
        record = _normalize(_read_json(path))
        if record is None:
            continue

        record['origin'] = root.origin
        record['origin_label'] = root.label
        records.append(record)

    return records


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
        'started_at': _parse_started_at(data.get('startedAt')),
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


def _parse_started_at(value: Any) -> float | None:
    """Parse the ``startedAt`` field (epoch milliseconds) to a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return float(value) if value > 0 else None
