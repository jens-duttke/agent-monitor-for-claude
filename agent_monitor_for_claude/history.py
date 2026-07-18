"""
Session History
===============

Lists past, non-live sessions - the conversations that still have a transcript
under ``projects/<slug>/<session>.jsonl`` but no longer have a process running.
These are the sessions Claude Code offers under ``--resume``; the live overview
(:func:`snapshot.build_snapshot`) never shows them because it is driven purely
by the session registry, which is pruned once a process exits.

This is a deliberately separate, on-demand path: the UI fetches it only when the
history filter is enabled, so the potentially large ``projects/`` scan (reading
each transcript once to resolve its correct title) never runs on the per-second
poll and never costs anything while the filter is off.

Parsing degrades gracefully, exactly like the live path: an unreadable file or
project directory is skipped, never raised.  Everything returned is
JSON-serializable and free of conversation content.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import cwd_to_slug, projects_dir
from .sessions import list_sessions
from .snapshot import live_or_recent_ids
from .transcript import history_state_for

__all__ = ['list_history']


def list_history() -> list[dict[str, Any]]:
    """Return raw records for every past (non-live) session transcript.

    Sessions the live snapshot still retains are omitted: those are the live (or
    just-ended, within the retention window) sessions the regular snapshot
    already shows, so listing them here as well would double them up.  The dedup
    is against exactly that retained set (:func:`snapshot.live_or_recent_ids`),
    not every registry record - a dead session whose registry record was never
    pruned and whose activity is past the retention window is dropped by the live
    snapshot, so it belongs here rather than nowhere.  Each returned record
    mirrors the shape the UI's ``buildSession`` consumes, marked ``is_history``
    and always ``alive: False`` (the process is gone, so the derived status is
    ``completed``).

    The working directory that groups a session under its project is resolved
    **per project folder**, not per transcript: a minimal or aborted transcript
    can carry no ``cwd`` of its own, but every transcript in one
    ``projects/<slug>/`` folder belongs to the same project, so a cwd-less
    session inherits the folder's real path from a sibling transcript (or the
    live registry).  Without this, those sessions would fall back to the raw
    slug and split off into a separate, slug-named panel instead of grouping
    with the rest of their project.
    """
    root = projects_dir()
    if not root.is_dir():
        return []

    live = list_sessions()
    live_ids = live_or_recent_ids()

    # The live registry is the authority on a project's exact cwd (it is what the
    # live snapshot groups by), so prefer it; first writer wins per slug. Keyed
    # case-insensitively: the drive-letter (and any) casing of a cwd can differ
    # from the on-disk folder name (Windows paths are case-insensitive), so a
    # case-sensitive match would miss - the same reason groupKey lowercases.
    slug_to_cwd: dict[str, str] = {}
    for record in live:
        slug_to_cwd.setdefault(cwd_to_slug(record['cwd']).lower(), record['cwd'])

    try:
        project_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for project_dir in project_dirs:
        try:
            transcripts = sorted(project_dir.glob('*.jsonl'))
        except OSError:
            continue

        folder_records: list[dict[str, Any]] = []
        for path in transcripts:
            if path.stem in live_ids:
                continue

            record = _build_history_record(path)
            if record is not None:
                folder_records.append(record)

        if not folder_records:
            continue

        canonical_cwd = _resolve_folder_cwd(project_dir.name, folder_records, slug_to_cwd)
        for record in folder_records:
            if not record['cwd']:
                record['cwd'] = canonical_cwd

        records.extend(folder_records)

    return records


def _resolve_folder_cwd(slug: str, folder_records: list[dict[str, Any]], slug_to_cwd: dict[str, str]) -> str:
    """Return the project cwd shared by every session in one project folder.

    Prefers the live registry's exact cwd for the slug (so history merges into
    the live panel), then any sibling transcript's own cwd, and only if neither
    exists falls back to the raw slug.
    """
    if slug.lower() in slug_to_cwd:
        return slug_to_cwd[slug.lower()]

    for record in folder_records:
        if record['cwd']:
            return record['cwd']

    return _cwd_from_slug(slug)


def _build_history_record(path: Path) -> dict[str, Any] | None:
    """Assemble one raw history record from a transcript, or None on failure.

    The ``cwd`` may be ``None`` here (a minimal transcript carries none); the
    caller fills it in per folder.
    """
    try:
        state = history_state_for(path)
    except Exception:
        # Last-resort per-file isolation, mirroring build_snapshot: one bad
        # transcript must skip that entry, never blank the whole history list.
        return None

    return {
        'is_history': True,
        'alive': False,
        'has_transcript': True,
        'session_id': state.session_id,
        'cwd': state.cwd,
        'short_name': state.session_id[:8],
        'kind': 'interactive',
        'entrypoint': None,
        'native_status': None,
        'waiting_for': None,
        'child_count': 0,
        'host': None,
        'via_cli': False,
        'has_activity': state.age_seconds is not None,
        'last_entry_kind': None,
        'last_stop_reason': None,
        'usage_limited': False,
        'pending_tool': False,
        'last_tool_name': None,
        'permission_mode': None,
        'model_id': state.model,
        'usage': {},
        'usage_by_model': {},
        'model_timeline': [],
        'title': state.title,
        'subagents_running': 0,
        'subagents_done': 0,
        'subagents_labels': [],
        'age_seconds': state.age_seconds,
    }


def _cwd_from_slug(slug: str) -> str:
    """Fallback project label when a transcript carries no ``cwd`` of its own.

    The slug is a lossy transform of the original path (every non-alphanumeric
    character became a hyphen), so it cannot be reversed to the real directory.
    It is returned verbatim as a stable, if unlovely, grouping key so sessions
    in the same project folder still group together.
    """
    return slug
