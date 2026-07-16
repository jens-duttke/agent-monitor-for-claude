"""
Session Deletion
================

Deletes a past session's on-disk transcript and its subagent folder.  This is
the application's **only** sanctioned file-deletion surface: everything else is
strictly read-only.  It exists so the UI can offer a "delete" action for the
history listing (past, non-live sessions), removing an old conversation from
disk - and thus from Claude Code's ``--resume`` list - for good.

Three guards keep it safe:

* the session id must be a UUID, so nothing but a well-formed session file name
  can ever be targeted;
* it refuses outright if the session currently has a **live** process (a
  race-condition guard: a session that started up between the UI listing it and
  the click must never have its files pulled out from under a running Claude
  Code);
* the computed paths are confined to ``projects/`` - a stale path, a traversal
  attempt, or anything resolving outside is a no-op.

Only file removal happens here; there is no other side effect.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .paths import cwd_to_slug, projects_dir
from .process_probe import probe_all
from .sessions import list_sessions

__all__ = ['delete_session']

# A session id is always a UUID; strict validation means only a real session
# transcript file name can be formed, never an arbitrary path.
_SESSION_ID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')


def delete_session(session_id: str, cwd: str) -> bool:
    """Delete a past session's transcript and subagent folder (user-initiated).

    Refuses - returning ``False`` without touching anything - when the session
    id is not a UUID, when the session currently has a live process, or when the
    computed paths would fall outside ``projects/``.

    Parameters
    ----------
    session_id : str
        The session's UUID (its transcript file stem).
    cwd : str
        The session's working directory, used to locate its project folder.

    Returns
    -------
    bool
        True if the transcript (and any subagent folder) were removed or were
        already absent; False if a guard rejected the request or a file could
        not be removed (e.g. still locked).
    """
    if not _is_valid_session_id(session_id) or not isinstance(cwd, str) or not cwd:
        return False

    if _is_live(session_id):
        return False

    try:
        root = projects_dir().resolve()
    except OSError:
        # A resolve failure (a reparse/symlink loop, an uncanonicalizable path)
        # must degrade to a graceful refusal, never crash the bridge call.
        return False

    slug = cwd_to_slug(cwd)
    transcript = root / slug / f'{session_id}.jsonl'
    session_dir = root / slug / session_id

    if not _within(root, transcript) or not _within(root, session_dir):
        return False

    try:
        if transcript.is_file():
            transcript.unlink()
        if session_dir.is_dir():
            shutil.rmtree(session_dir)
    except OSError:
        return False

    return True


def _is_valid_session_id(session_id: object) -> bool:
    """Return True if *session_id* is a well-formed UUID string."""
    return isinstance(session_id, str) and bool(_SESSION_ID_PATTERN.match(session_id))


def _is_live(session_id: str) -> bool:
    """Return True if a registry session with this id has a live process.

    The registry is the authority on liveness; a session absent from it (the
    common history case) has no process and is safe to delete.  Re-reading it
    here, immediately before deletion, closes the window between the UI listing
    a session and the user clicking delete.
    """
    records = [record for record in list_sessions() if record['session_id'] == session_id]
    if not records:
        return False

    probe_map = probe_all([(record['pid'], record['proc_start_ticks']) for record in records])
    return any(probe_map[record['pid']].alive for record in records)


def _within(root: Path, candidate: Path) -> bool:
    """Return True if *candidate* resolves to a path inside *root*."""
    try:
        candidate.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False
