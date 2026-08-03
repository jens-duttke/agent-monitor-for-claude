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

Every session root (the native Windows install, plus one per running WSL
distro - see ``roots.session_roots``) can be deleted from: the caller passes
the ``origin`` its UI row was tagged with (see ``sessions.list_sessions``),
resolved here through ``roots.root_for_origin`` - a refusal, never a fallback,
so an origin that no longer names a currently discovered root (most often a
WSL distro that has since stopped running) makes the session unreachable
rather than silently deleting from the wrong root. The live-process guard is
root-wide too: a session id is checked against every currently available
root, each probed through its own kind (see ``_is_live``).

Only file removal happens here; there is no other side effect.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .paths import cwd_to_slug, projects_dir
from .process_probe import probe_all
from .roots import root_for_origin, session_roots
from .sessions import list_sessions
from .wsl import probe_wsl_sessions

__all__ = ['delete_session']

# A session id is always a UUID; strict validation means only a real session
# transcript file name can be formed, never an arbitrary path.
_SESSION_ID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')


def delete_session(session_id: str, cwd: str, origin: str = 'windows') -> bool:
    """Delete a past session's transcript and subagent folder (user-initiated).

    Refuses - returning ``False`` without touching anything - when the session
    id is not a UUID, when *origin* does not name a currently available session
    root, when the session currently has a live process on any root, or when
    the computed paths would fall outside that root's ``projects/``. *origin*
    is resolved before any liveness check or filesystem access is attempted, so
    an unknown origin never causes a probe or a read.

    Parameters
    ----------
    session_id : str
        The session's UUID (its transcript file stem).
    cwd : str
        The session's working directory, used to locate its project folder.
    origin : str
        The session root the row was tagged with (``'windows'`` or
        ``'wsl:<distro>'`` - see ``paths.SessionRoot.origin``); defaults to
        ``'windows'`` for callers that predate multi-root support.

    Returns
    -------
    bool
        True if the transcript (and any subagent folder) were removed or were
        already absent; False if a guard rejected the request or a file could
        not be removed (e.g. still locked).
    """
    if not _is_valid_session_id(session_id) or not isinstance(cwd, str) or not cwd:
        return False

    root = root_for_origin(origin)
    if root is None:
        return False

    if _is_live(session_id):
        return False

    try:
        root_dir = projects_dir(root).resolve()
    except OSError:
        # A resolve failure (a reparse/symlink loop, an uncanonicalizable path)
        # must degrade to a graceful refusal, never crash the bridge call.
        return False

    slug = cwd_to_slug(cwd)
    transcript = root_dir / slug / f'{session_id}.jsonl'
    session_dir = root_dir / slug / session_id

    if not _within(root_dir, transcript) or not _within(root_dir, session_dir):
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
    """Return True if a registry session with this id has a live process, on any session root.

    Every currently available root (``roots.session_roots``) is checked in
    turn: the records naming *session_id* in that root's registry are probed
    through that root's own kind - ``process_probe.probe_all`` for the Windows
    root, ``wsl.probe_wsl_sessions`` for a WSL root - so a pid is never looked
    up against another root's process table. A root whose registry simply does
    not name *session_id* contributes nothing and the scan moves on to the next
    root; re-reading the registry here, immediately before deletion, closes the
    window between the UI listing a session and the user clicking delete.

    This is a deletion guard, so an error is never treated as "not live": if a
    root's registry does name *session_id* but reading that root's registry or
    probing it then raises, the session is reported live (deletion refused)
    rather than risking a false "dead" from an unreadable or momentarily
    unreachable root - a dropped WSL mount, for example. Only a root that was
    actually readable and genuinely does not name the id stays silent.

    Parameters
    ----------
    session_id : str
        The session id being considered for deletion.
    """
    for root in session_roots():
        try:
            records = [record for record in list_sessions(root) if record['session_id'] == session_id]
            if not records:
                continue

            requests = [(record['pid'], record['proc_start_ticks']) for record in records]
            probe_map = probe_all(requests) if root.proc_dir is None else probe_wsl_sessions(root, requests)
        except Exception:
            return True

        if any(probe_map[record['pid']].alive for record in records):
            return True

    return False


def _within(root: Path, candidate: Path) -> bool:
    """Return True if *candidate* resolves to a path inside *root*."""
    try:
        candidate.resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False
