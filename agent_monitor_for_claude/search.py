"""
Session Content Search
=======================

Finds the sessions whose transcript contains a search string.  Unlike the live
snapshot and the history listing - both of which are deliberately content-free -
this is the one path that reads conversation text, so it is tightly
encapsulated: it runs only on an explicit, on-demand user search, reads each
transcript locally, and reports **only the matching session ids**.  No message
text, no snippet, no line ever crosses the bridge, reaches the UI, or leaves the
machine (there is no network anywhere in this application).

The query supports the familiar editor options (mirroring VS Code): match case,
whole word, and regular expression.  All three are compiled into one Python
regular expression, and matching is **line-based** (each JSONL line is one
transcript entry), which is both what an editor's search means and what keeps a
regex match well-defined - no chunk-boundary reassembly.  An invalid regular
expression is reported back as an error so the UI can flag it, without ever
scanning a file.

The search is streaming, not batch: it reports progress and matches through an
``on_update`` callback as it goes, so the UI can drive a progress bar and fill in
results live.  Work is ordered and shaped for the on-demand call, not the
per-second poll:

- The caller passes the exact set of sessions in view, so only those transcripts
  are read - a handful when the history filter is off, more when it is on.  The
  ``projects/`` tree is never walked here.
- Transcripts are scanned **newest file first** (by modification time), so the
  most recently active sessions - the ones most likely wanted - surface first.
- Each transcript is read line by line and abandoned at the first hit; files are
  scanned concurrently on a thread pool, and matches are emitted strictly in
  newest-first order.
- ``should_cancel`` is polled throughout (per file and per line), so a superseded
  search - the user typed another character - stops promptly.

Parsing degrades gracefully like every other reader here: an unreadable or
missing transcript is skipped, never raised.  Every path is confined to
``projects/`` (resolved and checked), so a crafted id or cwd can never point the
read outside the transcript tree.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .paths import projects_dir, transcript_path

__all__ = ['run_search']

# A pathologically long query cannot usefully match and would only waste work;
# ignore it (the UI never sends one). Regex patterns can be a little longer than
# a plain phrase, hence the generous bound.
_MAX_QUERY_LEN = 500

# I/O-bound work, so oversubscribe the cores; still capped so a huge in-view set
# does not spawn an unbounded number of threads.
_MAX_WORKERS = 32

# Matches are coalesced into small batches before being reported, so a query that
# hits many sessions does not fire one update per match while still feeling live.
_BATCH_SIZE = 8

# How often to report progress even while no match is found, so the progress bar
# still advances during a long stretch of non-matching files.
_PROGRESS_EVERY = 25


def run_search(
    query: object,
    sessions: object,
    options: object,
    on_update: Callable[[int, int, list[str], bool, bool], None],
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Scan the given sessions' transcripts for ``query``, reporting progress live.

    Parameters
    ----------
    query
        The search string.  A blank, non-string, or over-long value matches
        nothing (only a final, complete update is reported, without error).
    sessions
        The sessions currently in view, each a mapping with ``session_id`` and
        ``cwd``.  Only these transcripts are read, so the scope and cost are
        exactly what the user can see.
    options
        A mapping with the boolean search options ``match_case``, ``whole_word``
        and ``use_regex`` (mirroring the editor toggles).  Absent keys are false.
    on_update
        Called as ``(processed, total, matches, done, error)``: how many
        transcripts have been scanned, how many in all, any newly matched session
        ids since the last update (newest first, each once), whether the scan has
        finished, and whether the query was an invalid regular expression.
        ``matches`` carries only ids - never any content.  A final call always
        arrives with ``done=True``.
    should_cancel
        Polled throughout; when it returns true the scan stops as soon as
        possible (no further updates).
    """
    cancel = should_cancel if callable(should_cancel) else _never

    matcher, invalid = _compile_matcher(query, options)
    if invalid:
        on_update(0, 0, [], True, True)
        return
    if matcher is None:
        on_update(0, 0, [], True, False)
        return

    ordered = _ordered_transcripts(sessions)
    total = len(ordered)
    if total == 0:
        on_update(0, 0, [], True, False)
        return

    def check(item: tuple[Path, str]) -> str | None:
        path, session_id = item
        if cancel():
            return None
        return session_id if _file_contains(path, matcher, cancel) else None

    workers = min(_MAX_WORKERS, max(1, (os.cpu_count() or 4) * 4), total)
    processed = 0
    pending: list[str] = []
    seen: set[str] = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map yields in submission order, so matches are emitted strictly
        # newest-file-first even though the scans run concurrently.
        for result in pool.map(check, ordered):
            if cancel():
                return

            processed += 1
            if result is not None and result not in seen:
                seen.add(result)
                pending.append(result)

            if len(pending) >= _BATCH_SIZE or processed % _PROGRESS_EVERY == 0:
                on_update(processed, total, pending, False, False)
                pending = []

    if not cancel():
        on_update(processed, total, pending, True, False)


def _never() -> bool:
    return False


def _compile_matcher(query: object, options: object) -> tuple[re.Pattern[str] | None, bool]:
    """Compile the query + options into a regex.

    Returns ``(pattern, invalid)``: a compiled pattern when the query is usable,
    ``(None, False)`` when it is blank or over-long (no filter, not an error), or
    ``(None, True)`` when regular-expression mode was on and the pattern would not
    compile (an error the UI flags).
    """
    if not isinstance(query, str):
        return None, False

    text = query.strip()
    if not text or len(text) > _MAX_QUERY_LEN:
        return None, False

    opts = options if isinstance(options, dict) else {}
    use_regex = bool(opts.get('use_regex'))
    whole_word = bool(opts.get('whole_word'))
    match_case = bool(opts.get('match_case'))

    pattern = text if use_regex else re.escape(text)
    if whole_word:
        pattern = r'\b(?:' + pattern + r')\b'

    flags = 0 if match_case else re.IGNORECASE
    try:
        return re.compile(pattern, flags), False
    except re.error:
        return None, True


def _ordered_transcripts(sessions: object) -> list[tuple[Path, str]]:
    """Return ``(path, session_id)`` for each in-view transcript, newest first.

    Sorted by file modification time, most recent first, so the freshest
    sessions are scanned - and their matches reported - before older ones.
    """
    refs = _valid_refs(sessions)
    if not refs:
        return []

    try:
        root = projects_dir().resolve()
    except OSError:
        return []

    items: list[tuple[float, Path, str]] = []
    for session_id, cwd in refs:
        path = _confined_transcript(session_id, cwd, root)
        if path is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        items.append((mtime, path, session_id))

    items.sort(key=lambda item: item[0], reverse=True)
    return [(path, session_id) for _mtime, path, session_id in items]


def _valid_refs(sessions: object) -> list[tuple[str, str]]:
    """Extract distinct ``(session_id, cwd)`` pairs from the caller's list."""
    if not isinstance(sessions, list):
        return []

    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in sessions:
        if not isinstance(item, dict):
            continue

        session_id = item.get('session_id')
        cwd = item.get('cwd')
        if not (isinstance(session_id, str) and session_id and isinstance(cwd, str) and cwd):
            continue

        pair = (session_id, cwd)
        if pair not in seen:
            seen.add(pair)
            refs.append(pair)

    return refs


def _confined_transcript(session_id: str, cwd: str, root: Path) -> Path | None:
    """Resolve a session's transcript path, or ``None`` if it escapes ``projects/``.

    The path is confined the same way the deletion surface is: it is resolved and
    checked to sit under ``projects/``, so a crafted id or cwd carrying path
    traversal can never point the read at a file outside the transcript tree.
    """
    try:
        path = transcript_path(session_id, cwd).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None

    return path if path.is_file() else None


def _file_contains(path: Path, matcher: re.Pattern[str], should_cancel: Callable[[], bool]) -> bool:
    """True if any line of the transcript matches ``matcher``.

    Read line by line (a JSONL line is one transcript entry, so matching is
    line-based like an editor's search) and abandoned at the first hit.
    ``should_cancel`` is polled per line so a superseded search stops promptly.
    Any read error yields ``False`` - a search never raises.
    """
    try:
        with path.open('r', encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                if should_cancel():
                    return False
                if matcher.search(line):
                    return True
    except OSError:
        return False

    return False
