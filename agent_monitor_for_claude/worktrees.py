"""
Worktree Sessions
=================

Files a session that switched into a git worktree back under the project it was
launched in.

The session registry reports a session's *current* working directory, so a
session that entered a worktree reports the worktree.  Everything else Claude
Code writes for it - the transcript, subagent transcripts, task output, the
scratchpad - stays under the project-folder slug of the directory it was
launched in.  Taken at face value, the registry ``cwd`` points at a folder that
holds nothing of this session: it would read as a never-used session, in a
panel of its own.

:func:`resolve_worktree` repairs the record before anything reads it.  When no
transcript sits under the registry ``cwd``, the transcript is looked up by the
session id across the root's project folders; if its newest ``worktree-state``
entry names a launch directory whose slug is the folder it was found in, the
record's ``cwd`` becomes that launch directory, and the worktree travels on as
``worktree_cwd``/``worktree_branch``.  Every reader downstream (transcript,
subagents, task output, scratchpad, search) then finds the files where Claude
Code put them, and the session is grouped under its project.

Whether the transcript sits under the registry ``cwd`` is answered by the
poll's :class:`~agent_monitor_for_claude.paths.TranscriptIndex`, one listing per
folder for every record in it.  A lookup costs one ``stat()`` per project
folder - too much for every poll - so its outcome is remembered in memory,
keyed like the registry record it answers for: a hit for as long as its
transcript is still there, a miss for
``_RETRY_SECONDS``.  A miss is retried rather than kept because it is usually a
new session that has no transcript yet, and because the registry can name the
worktree a moment before the transcript records the switch.  Nothing is written
to disk.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .paths import SessionRoot, TranscriptIndex, cwd_to_slug, find_transcript, transcript_path
from .transcript import worktree_for

__all__ = ['resolve_worktree', 'prune_worktree_cache']

# How long a failed lookup is trusted before the project folders are searched
# again for the same registry record.
_RETRY_SECONDS = 10.0


@dataclass(frozen=True)
class _Resolution:
    """The outcome of one lookup; ``project_cwd`` is None for a miss."""

    project_cwd: str | None
    branch: str | None
    checked_at: float


# (origin, session_id, registry cwd) -> the lookup's outcome.
_resolutions: dict[tuple[str, str, str], _Resolution] = {}

# The snapshot, the change fingerprint and the history dedupe each collect the
# registry, on different pywebview worker threads.
_lock = threading.Lock()


def resolve_worktree(root: SessionRoot, record: dict[str, Any], index: TranscriptIndex | None = None) -> dict[str, Any]:
    """Return *record* filed under its launch project when its session works in a worktree, else *record* unchanged.

    Parameters
    ----------
    root : SessionRoot
        The root the registry record was read from.
    record : dict
        One record as :func:`sessions.list_sessions` returns it.
    index : TranscriptIndex or None
        The poll's listing of the project folders, shared by every record of
        that poll; a fresh one when omitted.

    Returns
    -------
    dict
        *record* itself, or a copy whose ``cwd`` is the launch directory and
        which carries ``worktree_cwd`` (the registry's ``cwd``) and
        ``worktree_branch`` (or None when the transcript names no branch for
        that worktree).
    """
    index = index or TranscriptIndex()
    session_id = record['session_id']
    cwd = record['cwd']
    if index.stat(root, session_id, cwd) is not None:
        return record

    key = (root.origin, session_id, cwd)
    with _lock:
        resolution = _resolutions.get(key)

    if resolution is None or not _still_valid(root, session_id, resolution, index):
        resolution = _locate(root, session_id, cwd)
        with _lock:
            _resolutions[key] = resolution

    if resolution.project_cwd is None:
        return record

    return {**record, 'cwd': resolution.project_cwd, 'worktree_cwd': cwd, 'worktree_branch': resolution.branch}


def prune_worktree_cache(active: Iterable[tuple[str, str, str]]) -> None:
    """Drop lookups for registry records that no longer exist, mirroring ``transcript.prune_scan_cache``.

    Parameters
    ----------
    active : iterable of (origin, session_id, registry cwd)
        Every current registry record across every session root, keyed by the
        ``cwd`` the registry reports - for a resolved record that is its
        ``worktree_cwd``, not the launch directory it was filed under.
    """
    keep = set(active)

    with _lock:
        for key in [key for key in _resolutions if key not in keep]:
            del _resolutions[key]


def _still_valid(root: SessionRoot, session_id: str, resolution: _Resolution, index: TranscriptIndex) -> bool:
    """Return True if a remembered lookup can be reused without searching again."""
    if resolution.project_cwd is None:
        return time.monotonic() - resolution.checked_at < _RETRY_SECONDS

    return index.stat(root, session_id, resolution.project_cwd) is not None


def _locate(root: SessionRoot, session_id: str, cwd: str) -> _Resolution:
    """Search for the session's transcript and read the launch directory from its worktree state."""
    now = time.monotonic()
    miss = _Resolution(project_cwd=None, branch=None, checked_at=now)

    path = find_transcript(root, session_id)
    if path is None:
        return miss

    worktree = worktree_for(path)
    if worktree is None:
        return miss

    project_cwd = _launch_cwd_for(root, session_id, worktree.launch_cwds, path.parent.name)
    if project_cwd is None:
        return miss

    branch = worktree.branch if os.path.normcase(worktree.path) == os.path.normcase(cwd) else None
    return _Resolution(project_cwd=project_cwd, branch=branch, checked_at=now)


def _launch_cwd_for(root: SessionRoot, session_id: str, launch_cwds: tuple[str, ...], folder_name: str) -> str | None:
    """Return the launch directory whose slug names the transcript's folder *folder_name*, or None if none does.

    An exact match wins, so every path later built from the directory is the
    one on disk.  Failing that, a launch directory whose transcript path the
    file system resolves is accepted: Claude Code records a drive letter in
    either case, which a case-insensitive file system treats as the same
    folder and a case-sensitive one rightly does not.  Returning None leaves
    the record as the registry wrote it, rather than filing it under a
    directory that does not hold its transcript.
    """
    for launch_cwd in launch_cwds:
        if cwd_to_slug(launch_cwd) == folder_name:
            return launch_cwd

    for launch_cwd in launch_cwds:
        if transcript_path(root, session_id, launch_cwd).is_file():
            return launch_cwd

    return None
