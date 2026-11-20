"""
Claude Desktop groups
=====================

Reads, never writes, how Claude Desktop sorts its Code sessions: the manual
groups with their order and collapsed state, which session sits in which group,
which sessions are pinned and which are archived.

The groups live in ``claude_desktop_config.json`` (``preferences.epitaxyPrefs``,
under ``dframe-group-scopes`` and ``dframe-code-sections``, one entry per
account/organization scope), the pinned sessions under
``starred-local-code-sessions`` (one list of ``local_<desktopId>`` ids).  Each Desktop session has its own file
``claude-code-sessions/<account>/<org>/local_<desktopId>.json`` that names the
Claude Code session (``cliSessionId``, or the one it ran last once its process
has ended) and whether it is archived.  Every file
is parsed once and remembered by its modification time and size, so a poll
reads only what changed.  The format is an undocumented internal of Claude
Desktop, so every shape is checked and a surprise degrades to "no groups".
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .platforms import desktop_data_dirs

__all__ = ['desktop_groups']

_CONFIG_FILE = 'claude_desktop_config.json'
_SESSIONS_DIR = 'claude-code-sessions'
_SESSION_FILE_PREFIX = 'local_'
_ASSIGNMENT_PREFIX = 'code:'
_PREFERENCES_KEY = 'preferences'
_EPITAXY_KEY = 'epitaxyPrefs'
_SCOPES_KEY = 'dframe-group-scopes'
_SECTIONS_KEY = 'dframe-code-sections'
_STARRED_KEY = 'starred-local-code-sessions'
_MAX_GROUP_NAME_CHARS = 80
_CODE_MODE = 'code'
_MANUAL_KIND = 'manual'
_UUID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')
_STARRED_ID_PATTERN = re.compile(r'\Alocal_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')

# File path -> (mtime_ns, size, parsed JSON or None).
_cache: dict[str, tuple[int, int, Any]] = {}
_lock = threading.Lock()


def desktop_groups(dirs: list[Path] | None = None) -> dict[str, Any]:
    """Return Claude Desktop's session groups and archive flags.

    Parameters
    ----------
    dirs : list of Path, optional
        Desktop data directories to read; the installed ones by default.

    Returns
    -------
    dict
        ``groups`` (``{id, name, order, collapsed}`` in Desktop's order),
        ``members`` (Claude Code session id -> group id, archived sessions
        left out), ``archived`` (Claude Code session ids) and ``starred``
        (the Claude Code session ids pinned in Desktop, in Desktop's order,
        archived ones included).
    """
    directories = desktop_data_dirs() if dirs is None else dirs

    groups: list[dict[str, Any]] = []
    group_ids: set[str] = set()
    assignments: list[tuple[str, str]] = []
    starred_ids: list[str] = []
    cli_sessions: dict[str, tuple[str, bool]] = {}
    seen_files: set[str] = set()

    for directory in directories:
        preferences = _preferences(directory)
        for scope, scope_data in _dict_items(preferences.get(_SCOPES_KEY)):
            _collect_groups(scope_data, _scope_sections(preferences, scope), groups, group_ids)
            assignments.extend(_scope_assignments(scope_data))
        starred_ids.extend(_starred_ids(preferences))
        cli_sessions.update(_session_files(directory, seen_files))

    _prune_cache(seen_files)

    members: dict[str, str] = {}
    active_ids = {cli_id for cli_id, is_archived in cli_sessions.values() if not is_archived}
    archived: list[str] = []
    for cli_id, is_archived in cli_sessions.values():
        if is_archived and cli_id not in active_ids and cli_id not in archived:
            archived.append(cli_id)

    for desktop_id, group_id in assignments:
        session = cli_sessions.get(desktop_id)
        if session is None or session[1] or group_id not in group_ids:
            continue
        members[session[0]] = group_id

    starred: list[str] = []
    for desktop_id in starred_ids:
        session = cli_sessions.get(desktop_id)
        if session is not None and session[0] not in starred:
            starred.append(session[0])

    for position, group in enumerate(groups):
        group['order'] = position

    return {'groups': groups, 'members': members, 'archived': archived, 'starred': starred}


def _preferences(directory: Path) -> dict[str, Any]:
    """The ``epitaxyPrefs`` object of one directory's Desktop config, or an empty dict."""
    config = _cached_json(directory / _CONFIG_FILE)
    preferences = config.get(_PREFERENCES_KEY) if isinstance(config, dict) else None
    epitaxy = preferences.get(_EPITAXY_KEY) if isinstance(preferences, dict) else None
    return epitaxy if isinstance(epitaxy, dict) else {}


def _dict_items(value: Any) -> list[tuple[str, dict[str, Any]]]:
    """The ``(key, object)`` pairs of *value* whose value is an object; anything else yields nothing."""
    if not isinstance(value, dict):
        return []

    return [(key, item) for key, item in value.items() if isinstance(key, str) and isinstance(item, dict)]


def _scope_sections(preferences: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    """One scope's manual sections in Desktop's display order."""
    scope_sections = preferences.get(_SECTIONS_KEY)
    entry = scope_sections.get(scope) if isinstance(scope_sections, dict) else None
    sections = entry.get('sections') if isinstance(entry, dict) else None
    if not isinstance(sections, list):
        return []

    manual = [section for section in sections if isinstance(section, dict) and section.get('kind') == _MANUAL_KIND]
    return sorted(manual, key=_section_order)


def _section_order(section: dict[str, Any]) -> float:
    order = section.get('order')
    return float(order) if isinstance(order, (int, float)) and not isinstance(order, bool) else float('inf')


def _collect_groups(scope_data: dict[str, Any], sections: list[dict[str, Any]], groups: list[dict[str, Any]], group_ids: set[str]) -> None:
    """Append one scope's Code groups to *groups*: those with a section first, in section order, then the rest."""
    declared = scope_data.get('groups')
    if not isinstance(declared, list):
        return

    by_id: dict[str, dict[str, Any]] = {}
    for group in declared:
        if not isinstance(group, dict) or not isinstance(group.get('id'), str) or group.get('mode', _CODE_MODE) != _CODE_MODE:
            continue
        by_id.setdefault(group['id'], group)

    collapsed_by_id = {section['id']: section.get('collapsed') is True for section in sections if isinstance(section.get('id'), str)}
    ordered_ids = [section['id'] for section in sections if isinstance(section.get('id'), str) and section['id'] in by_id]
    ordered_ids += [group_id for group_id in by_id if group_id not in ordered_ids]

    for group_id in ordered_ids:
        if group_id in group_ids:
            continue

        group_ids.add(group_id)
        name = by_id[group_id].get('name')
        groups.append({
            'id': group_id,
            'name': name[:_MAX_GROUP_NAME_CHARS] if isinstance(name, str) else '',
            'order': 0,
            'collapsed': collapsed_by_id.get(group_id, False),
        })


def _scope_assignments(scope_data: dict[str, Any]) -> list[tuple[str, str]]:
    """The ``(desktop session id, group id)`` pairs of one scope; the id is the session file's stem."""
    assignments = scope_data.get('assignments')
    if not isinstance(assignments, dict):
        return []

    pairs: list[tuple[str, str]] = []
    for key, group_id in assignments.items():
        if isinstance(key, str) and isinstance(group_id, str) and key.startswith(_ASSIGNMENT_PREFIX):
            pairs.append((key[len(_ASSIGNMENT_PREFIX):], group_id))

    return pairs


def _starred_ids(preferences: dict[str, Any]) -> list[str]:
    """The Desktop session ids pinned in Desktop (``local_<uuid>``, the session file's stem); anything else is skipped."""
    starred = preferences.get(_STARRED_KEY)
    if not isinstance(starred, list):
        return []

    return [item for item in starred if isinstance(item, str) and _STARRED_ID_PATTERN.match(item)]


def _session_files(directory: Path, seen_files: set[str]) -> dict[str, tuple[str, bool]]:
    """Desktop session id -> ``(Claude Code session id, archived)`` for every readable session file below *directory*."""
    found: dict[str, tuple[str, bool]] = {}
    for path in _list_session_files(directory / _SESSIONS_DIR):
        seen_files.add(str(path))
        data = _cached_json(path)
        if not isinstance(data, dict):
            continue

        cli_id = _cli_session_id(data)
        if cli_id is not None:
            found[path.stem] = (cli_id, data.get('isArchived') is True)

    return found


def _cli_session_id(data: dict[str, Any]) -> str | None:
    """The Claude Code session a Desktop session file names, or None.

    Desktop clears ``cliSessionId`` once the chat's process has ended; the
    session it last ran is then ``lastAssistantCliSessionId``, else the newest
    of ``priorCliSessionIds``.
    """
    candidates: list[object] = [data.get('cliSessionId'), data.get('lastAssistantCliSessionId')]
    prior = data.get('priorCliSessionIds')
    if isinstance(prior, list) and prior:
        candidates.append(prior[-1])

    for candidate in candidates:
        if isinstance(candidate, str) and _UUID_PATTERN.match(candidate):
            return candidate

    return None


def _list_session_files(sessions_root: Path) -> list[Path]:
    """The ``local_*.json`` files two levels below *sessions_root* (account, organization)."""
    files: list[Path] = []
    for account in _subdirectories(sessions_root):
        for organization in _subdirectories(account):
            try:
                with os.scandir(organization) as entries:
                    for entry in entries:
                        if entry.name.startswith(_SESSION_FILE_PREFIX) and entry.name.endswith('.json'):
                            files.append(Path(entry.path))
            except OSError:
                continue

    return files


def _subdirectories(directory: Path) -> list[Path]:
    try:
        with os.scandir(directory) as entries:
            return [Path(entry.path) for entry in entries if entry.is_dir()]
    except OSError:
        return []


def _cached_json(path: Path) -> Any:
    """The parsed JSON of *path*, read again only when its modification time or size changed; None when unreadable."""
    try:
        stat_result = path.stat()
    except OSError:
        return None

    key = str(path)
    with _lock:
        cached = _cache.get(key)

    if cached is not None and cached[0] == stat_result.st_mtime_ns and cached[1] == stat_result.st_size:
        return cached[2]

    data = _read_json(path)
    with _lock:
        _cache[key] = (stat_result.st_mtime_ns, stat_result.st_size, data)

    return data


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _prune_cache(seen_session_files: set[str]) -> None:
    """Forget session files that no longer exist (a deleted or moved Desktop session)."""
    with _lock:
        for key in [key for key in _cache if os.path.basename(key).startswith(_SESSION_FILE_PREFIX) and key not in seen_session_files]:
            del _cache[key]
