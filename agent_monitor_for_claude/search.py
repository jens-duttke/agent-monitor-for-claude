"""
Session Content Search
=======================

Finds the sessions whose transcript contains a search string, and shows where.
Unlike the live snapshot and the history listing - both of which are
deliberately content-free - this is the one path that reads conversation text
**and puts some of it on screen**: alongside each matching session id it reports
how many hits the transcript holds and a short excerpt around the first few of
them, so a result can be judged without opening the session.

What it hands back is bounded rather than open:

- Only the transcripts of the sessions the caller passes - the ones in view -
  are read.  The ``projects/`` tree is never walked here.
- At most ``_MAX_SNIPPETS`` excerpts per session cross the bridge, each clipped
  to ``_CONTEXT_CHARS`` characters either side of the hit and cut at the hit's
  own line, so no excerpt can grow into a transcript dump.
- Everything stays on this machine; there is no network anywhere in this
  application.

Matching runs on the **readable text** of each entry, not on the raw JSONL line.
Each line is parsed and a whitelist of fields is searched - message text,
thinking, tool inputs and tool results (see :func:`_entry_texts`) - so a hit is
always something a person wrote or read, never a uuid, a timestamp or the
project path that sits on every single line.  A line that does not parse falls
back to being searched as raw text, so a format this cannot read still finds
what is in it instead of silently finding nothing.

Parsing a line costs far more than testing one, so a cheap **prefilter** runs
against the raw line first and only a line that could match is parsed (see
:func:`_prefilter`).  The prefilter is built to allow for JSON's own escaping,
so it never hides a hit that the readable text would have produced.  Ahead of
it, a transcript is read in large blocks and each block is searched as bytes
for the longest piece of the query JSON writes unchanged (see :func:`_literal`):
only the lines around an occurrence are decoded at all, which is what lets a
query that matches almost nothing get through gigabytes in a few seconds.

The query supports the familiar editor options (mirroring VS Code): match case,
whole word, and regular expression.  All three are compiled into one Python
regular expression.  An invalid regular expression is reported back as an error
so the UI can flag it, without ever scanning a file.

The search is streaming, not batch: it reports progress and matches through an
``on_update`` callback as it goes, so the UI can drive a progress bar and fill in
results live.  Work is ordered and shaped for the on-demand call, not the
per-second poll:

- Each session ref carries the ``origin`` of the root it lives on (the native
  Windows install, or one running WSL distro); it is resolved with
  ``roots.root_for_origin``, a refusal rather than a fallback, so a ref whose
  origin no longer names a currently discovered root - most often a WSL distro
  that has since stopped - is silently dropped, never substituted with another
  root's transcript tree.
- Transcripts are scanned **newest file first** (by modification time) across
  every root combined, so the most recently active sessions - the ones most
  likely wanted - surface first regardless of which root they live on.
- Files are scanned concurrently on a small thread pool, and matches are
  emitted strictly in newest-first order - each as soon as every newer file is
  done, never held back for a batch while a large transcript is still read.
- The background scans that fill in the figures on start-up are paused while a
  search runs (``pause_background_scans``): both need the interpreter lock, and
  someone is waiting for the search.
- How far each transcript was read for the newest query is remembered in
  memory (see :class:`_ScanMemo`).  An active search is repeated whenever the
  snapshot changes, and since a transcript only grows, the repeat reads only
  what was appended.
- ``should_cancel`` is polled throughout (per file, per block and per tested
  line), so a superseded search - the user typed another character - stops
  promptly.

Parsing degrades gracefully like every other reader here: an unreadable or
missing transcript is skipped, never raised.  Every path is confined to its own
root's ``projects/`` (resolved and checked against that same root), so a
crafted id or cwd can never point the read outside its root's transcript tree -
and never at another root's tree either.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from .background_scans import pause_background_scans
from .paths import SessionRoot, projects_dir, transcript_path
from .roots import root_for_origin

__all__ = ['run_search']

# A pathologically long query cannot usefully match and would only waste work;
# ignore it (the UI never sends one). Regex patterns can be a little longer than
# a plain phrase, hence the generous bound.
_MAX_QUERY_LEN = 500

# The scan holds the interpreter lock for most of its work (searching a block of
# bytes does not release it), so more threads only interleave the files: with 32
# the newest file - the first one reported - finished near the end of the whole
# scan. A few threads still overlap the reads, which matters over a WSL share.
_MAX_WORKERS = 4

# Matches are coalesced into small batches before being reported, so a query that
# hits many sessions does not fire one update per match while still feeling live.
_BATCH_SIZE = 8

# The shortest gap between two progress-only updates, so the progress bar moves
# while a long stretch of files matches nothing without one update per file.
_PROGRESS_SECONDS = 0.1

# How much of a transcript is read and tested at once. Testing a whole block for
# the query's literal costs a fraction of testing each line on its own.
_CHUNK_BYTES = 4 << 20

# The shortest part of a query worth searching a block for when it is not the
# whole query: a one- or two-letter run occurs on nearly every line.
_MIN_LITERAL_CHARS = 3

# How many bytes before a scan's resume point are compared on the next scan, so a
# transcript rewritten in place is read afresh rather than resumed mid-line.
_TAIL_BYTES = 64

# The non-ASCII characters Python's case-insensitive matching treats as an ASCII
# letter's other case. Lowering a block of bytes only folds ASCII, so a block
# holding one of these is tested line by line instead (see _Literal). Pinned
# against `re` itself by test_the_fold_table_matches_the_regex_engine.
_ASCII_FOLD_EXTRAS = {'i': ('\u0130', '\u0131'), 'k': ('\u212a',), 's': ('\u017f',)}

# Characters with a meaning of their own in a regular expression. A pattern
# without any of them is a literal, and gets the same block search as a phrase.
_REGEX_METACHARS = frozenset('.^$*+?{}[]\\|()')

# How much text either side of a hit an excerpt carries. Enough to recognize the
# passage, far too little to reconstruct it - and cut at the hit's own line, so
# the figure is an upper bound rather than what is usually sent.
_CONTEXT_CHARS = 60

# How many excerpts one session contributes. Deliberately small: the row shows
# them all at once, and the hit count already says how much more there is.
_MAX_SNIPPETS = 5

# The hit itself is clipped too - a greedy regular expression can match a whole
# paragraph, which no single row should carry.
_MAX_MATCH_CHARS = 120

# How many matching entries one transcript is opened for. A hit count can only be
# had by reading the file to the end, and a query hitting nearly every line pays
# for parsing each of those lines - so counting stops here and the result is
# reported as partial ("N+" in the interface). Nobody reads a four-digit hit count
# precisely, and past this point the exact figure costs more than it says: over
# 1.5 GB of transcripts a common query took 13.6 s at 100 and 8.4 s at 50, with
# 85% of the sessions still reporting an exact count. Lowering it further is where
# the trade stops paying - at 20 the time is flat (a rare query has to read every
# file whatever the cap, which is the floor all of this sits on) while the exact
# counts keep falling.
_MAX_MATCH_ENTRIES = 50

# How deep a tool input is walked for strings, and how many it may contribute.
# Both guard against a pathological structure, never against ordinary tool calls.
_MAX_INPUT_DEPTH = 6
_MAX_INPUT_STRINGS = 64

# Control characters would reach the interface as invisible or layout-breaking
# glyphs, so an excerpt drops them (tab, newline and the rest of whitespace are
# collapsed separately, below).
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_WHITESPACE_RUN = re.compile(r'\s+')

# How JSON writes the characters it has to escape. The prefilter accepts either
# spelling so a query containing one still narrows the scan (see _prefilter).
_JSON_ESCAPES = {
    '"': r'(?:"|\\")',
    '\\': r'(?:\\|\\\\)',
    '\n': r'(?:\n|\\n)',
    '\r': r'(?:\r|\\r)',
    '\t': r'(?:\t|\\t)',
    '\b': r'(?:\x08|\\b)',
    '\f': r'(?:\x0c|\\f)',
}

# Regex metacharacters that make a raw-line prefilter unsound in regex mode:
# an anchor means something different on the JSON line than in the extracted
# text, and a backslash or a quote can spell an escape the raw line writes
# differently. A pattern carrying any of them is scanned without a prefilter.
_UNSAFE_REGEX_CHARS = '\\"^$'


def run_search(
    query: object,
    sessions: object,
    options: object,
    on_update: Callable[[int, int, list[dict[str, Any]], bool, bool], None],
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Scan the given sessions' transcripts for ``query``, reporting progress live.

    Parameters
    ----------
    query
        The search string.  A blank, non-string, or over-long value matches
        nothing (only a final, complete update is reported, without error).
    sessions
        The sessions currently in view, each a mapping with ``session_id``,
        ``cwd`` and (when known) ``origin`` - the session root it lives on.  An
        absent or non-string ``origin`` defaults to the native Windows root,
        matching every caller from before origin-tagging existed.  Only these
        transcripts are read, so the scope and cost are exactly what the user
        can see.
    options
        A mapping with the boolean search options ``match_case``, ``whole_word``
        and ``use_regex`` (mirroring the editor toggles).  Absent keys are false.
    on_update
        Called as ``(processed, total, matches, done, error)``: how many
        transcripts have been scanned, how many in all, any newly matched
        sessions since the last update (newest first, each once), whether the
        scan has finished, and whether the query was an invalid regular
        expression.  Each entry of ``matches`` is a mapping with the session's
        ``session_id``, its ``count`` of hits, up to ``_MAX_SNIPPETS``
        ``snippets`` around the first of them, and ``partial`` when the count
        stopped early (see :func:`_scan_file`).  A final call always arrives
        with ``done=True``.
    should_cancel
        Polled throughout; when it returns true the scan stops as soon as
        possible (no further updates).
    """
    cancel = should_cancel if callable(should_cancel) else _never

    compiled, invalid = _compile_query(query, options)
    if invalid:
        on_update(0, 0, [], True, True)
        return
    if compiled is None:
        # A cleared search box ends the search, and with it what was remembered.
        _forget()
        on_update(0, 0, [], True, False)
        return

    ordered = _ordered_transcripts(sessions)
    total = len(ordered)
    if total == 0:
        on_update(0, 0, [], True, False)
        return

    def check(item: tuple[Path, str]) -> dict[str, Any] | None:
        path, session_id = item
        if cancel():
            return None
        return _scan_file(path, session_id, compiled, cancel)

    workers = min(_MAX_WORKERS, total)
    processed = 0
    reported = 0
    last_report = time.monotonic()
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Someone is waiting for this answer, whereas the first full scans of the
    # transcripts only fill in figures, and both compete for the interpreter lock.
    with pause_background_scans(), ThreadPoolExecutor(max_workers=workers) as pool:
        # Collected in submission order, so matches are emitted strictly
        # newest-file-first even though the scans run concurrently.
        futures: list[Future[dict[str, Any] | None]] = [pool.submit(check, item) for item in ordered]
        for future in futures:
            # About to wait for a file still being read: report what is already
            # known first, so a match does not sit behind a large transcript.
            waiting = not future.done()
            if waiting and (pending or (processed > reported and time.monotonic() - last_report >= _PROGRESS_SECONDS)):
                if cancel():
                    return
                on_update(processed, total, pending, False, False)
                pending, reported, last_report = [], processed, time.monotonic()

            result = future.result()
            if cancel():
                return

            processed += 1
            if result is not None and result['session_id'] not in seen:
                seen.add(result['session_id'])
                pending.append(result)

            if len(pending) >= _BATCH_SIZE:
                on_update(processed, total, pending, False, False)
                pending, reported, last_report = [], processed, time.monotonic()

    if not cancel():
        on_update(processed, total, pending, True, False)


def _never() -> bool:
    return False


@dataclass(frozen=True)
class _Literal:
    """A piece of the query that every matching line must hold as plain bytes.

    ``needle`` is the piece as UTF-8, lowered when ``fold`` (case-insensitive)
    is set; a block is then lowered too before it is searched.  ``extras`` are
    the spellings - UTF-8 and lowercase ``\\u`` escape - of the non-ASCII
    characters the regex engine would still accept as one of the piece's
    letters (``_ASCII_FOLD_EXTRAS``): lowering bytes cannot see those, so a
    block holding one is tested line by line.
    """

    needle: bytes
    fold: bool
    extras: tuple[bytes, ...]

    def defeated_by(self, haystack: bytes) -> bool:
        """Whether *haystack* (the block as searched) holds a character the byte search would miss."""
        return any(extra in haystack for extra in self.extras)


@dataclass(frozen=True)
class _Query:
    """A compiled query: the matcher, its raw-line prefilter and its byte literal.

    ``key`` identifies what the query matches, so an incremental scan is only
    ever resumed for the same query.  ``prefilter`` and ``literal`` are each
    ``None`` when no sound one can be built.
    """

    matcher: re.Pattern[str]
    prefilter: re.Pattern[str] | None
    literal: _Literal | None

    @property
    def key(self) -> tuple[str, int]:
        return self.matcher.pattern, self.matcher.flags


def _compile_query(query: object, options: object) -> tuple[_Query | None, bool]:
    """Compile the query + options into a :class:`_Query`.

    Returns ``(query, invalid)``: a compiled query when the text is usable,
    ``(None, False)`` when it is blank or over-long (no filter, not an error),
    or ``(None, True)`` when regular-expression mode was on and the pattern
    would not compile (an error the UI flags).
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
        matcher = re.compile(pattern, flags)
    except re.error:
        return None, True

    # A regular expression without a single metacharacter matches exactly what
    # the same text does as a phrase, so it is filtered like one.
    plain = not use_regex or not any(char in _REGEX_METACHARS for char in text)

    return _Query(matcher, _prefilter(text, not plain, match_case), _literal(text, plain, match_case)), False


def _literal(text: str, plain: bool, match_case: bool) -> _Literal | None:
    """Pick the longest piece of the query a matching raw line must hold verbatim, or ``None``.

    Like the prefilter, this may only ever admit a superset.  A printable ASCII
    character other than a quote or a backslash is written unchanged by JSON, so
    a run of them found in the readable text is on the raw line as it stands -
    up to case, which ``fold`` covers for ASCII and ``extras`` for the few
    non-ASCII characters the regex engine counts as an ASCII letter's other case.
    Whole-word mode is ignored here, as in the prefilter.  A regular expression
    qualifies only when it is *plain*: without a single metacharacter.
    """
    if not plain:
        return None

    run = max(_literal_runs(text), key=len, default='')
    if not run or (len(run) < _MIN_LITERAL_CHARS and run != text):
        return None

    if match_case:
        return _Literal(run.encode('ascii'), False, ())

    # Each such character in both spellings a line can carry: as UTF-8, and as
    # the lowercase escape an ASCII-safe writer uses (the block is lowered too).
    extras: list[bytes] = []
    for letter in sorted(set(run.lower())):
        for char in _ASCII_FOLD_EXTRAS.get(letter, ()):
            extras.extend((char.encode('utf-8'), b'\\u%04x' % ord(char)))

    return _Literal(run.lower().encode('ascii'), True, tuple(extras))


def _literal_runs(text: str) -> Iterator[str]:
    """Yield each maximal run of *text* that JSON writes unchanged (printable ASCII, no quote or backslash)."""
    start = 0
    for index, char in enumerate(text):
        if ' ' <= char <= '~' and char not in '"\\':
            continue
        if index > start:
            yield text[start:index]
        start = index + 1

    if start < len(text):
        yield text[start:]


def _prefilter(text: str, use_regex: bool, match_case: bool) -> re.Pattern[str] | None:
    """Build a cheap pattern that no matching raw line can escape, or ``None``.

    Matching runs on each entry's extracted text, but parsing every line to get
    it costs far more than testing one.  So the raw line is tested first and only
    a line that could match is parsed - which is only sound if the prefilter
    never rejects a line whose readable text does match.  Three things could make it do so, and each is handled here rather
    than assumed away:

    - JSON escaping.  A quote, a backslash, a newline or a non-ASCII character
      is written differently on the line than it reads once parsed, so a plain
      query is rebuilt character by character with both spellings allowed.
    - Whole-word mode.  Deliberately **not** reproduced here, even though the
      matcher anchors on ``\\b``: an escaped spelling puts different characters
      at the hit's edges, so the word boundaries sit elsewhere on the raw line
      than in the text.  A query for a word spelled ``\\u00fcber`` on the line
      begins at a backslash, which is no more a word character than the quote
      before it - the boundary the matcher finds in the parsed text does not
      exist here, and anchoring would drop the line.  Leaving the anchors off
      makes the prefilter admit a superset, which is exactly what a prefilter
      is allowed to do; the matcher then applies the real word boundaries to
      the parsed text.
    - Regular expressions.  An anchor addresses a different string on the raw
      line, and an escape can be spelled in ways this cannot see through, so a
      pattern carrying one of ``_UNSAFE_REGEX_CHARS`` (or any non-ASCII
      character, whose escaped spelling a pattern cannot be rewritten to allow)
      gets no prefilter at all - correct, merely slower.
    """
    if use_regex:
        if not text.isascii() or any(char in text for char in _UNSAFE_REGEX_CHARS):
            return None
        if any(ord(char) < 0x20 for char in text):
            return None
        pattern = text
    else:
        alternatives = [_json_alternative(char, match_case) for char in text]
        if any(alternative is None for alternative in alternatives):
            return None
        pattern = ''.join(alternative for alternative in alternatives if alternative is not None)

    try:
        return re.compile(pattern, 0 if match_case else re.IGNORECASE)
    except re.error:
        return None


def _json_alternative(char: str, match_case: bool) -> str | None:
    """Match *char* whether the raw line spells it literally or JSON-escaped.

    ``None`` for a character outside the basic multilingual plane, whose escaped
    spelling is a surrogate pair this does not try to reproduce - the caller then
    drops the prefilter rather than risk hiding a hit.
    """
    escaped = _JSON_ESCAPES.get(char)
    if escaped is not None:
        return escaped

    if char.isascii():
        # The regex engine also takes a few non-ASCII characters for an ASCII
        # letter's other case; written ASCII-safe, those are an escape.
        extras = () if match_case else _ASCII_FOLD_EXTRAS.get(char.lower(), ())
        if not extras:
            return re.escape(char)
        return '(?:' + '|'.join([re.escape(char)] + [r'\\u%04x' % ord(extra) for extra in extras]) + ')'

    if ord(char) > 0xFFFF:
        return None

    # A non-ASCII character survives JSON.stringify unescaped, but a writer using
    # ASCII-safe output would spell it \uXXXX - allow both. Without match case the
    # other case's escape is a distinct code point, so it is listed too.
    variants = {char} if match_case else {char.lower(), char.upper()}
    spellings = [re.escape(char)]
    for variant in sorted(variants):
        if len(variant) == 1 and ord(variant) <= 0xFFFF:
            spellings.append(r'\\u%04x' % ord(variant))

    return '(?:' + '|'.join(spellings) + ')'


def _ordered_transcripts(sessions: object) -> list[tuple[Path, str]]:
    """Return ``(path, session_id)`` for each in-view transcript, newest first, across every root.

    Sorted by file modification time, most recent first, so the freshest
    sessions are scanned - and their matches reported - before older ones,
    regardless of which root they came from.  Each ref's root is resolved by
    its own ``origin`` (see :func:`_valid_refs`); that resolution, and the
    root's own ``projects/`` directory, are each looked up only once per
    distinct origin seen in *sessions* (see :func:`_resolve_roots`), never once
    per ref.  An origin that resolves to no currently discovered root drops
    every ref that named it - a refusal, never a fallback to another root.
    """
    refs = _valid_refs(sessions)
    if not refs:
        return []

    roots_by_origin = _resolve_roots([origin for _session_id, _cwd, origin in refs])

    items: list[tuple[float, Path, str]] = []
    for session_id, cwd, origin in refs:
        resolved = roots_by_origin[origin]
        if resolved is None:
            continue
        root, projects_root = resolved

        path = _confined_transcript(session_id, cwd, root, projects_root)
        if path is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        items.append((mtime, path, session_id))

    items.sort(key=lambda item: item[0], reverse=True)
    return [(path, session_id) for _mtime, path, session_id in items]


def _valid_refs(sessions: object) -> list[tuple[str, str, str]]:
    """Extract distinct ``(session_id, cwd, origin)`` triples from the caller's list.

    ``origin`` defaults to ``'local'`` when absent or not a string, so a
    caller that does not yet tag its sessions with an origin - every ref shape
    from before origin-tagging existed - is treated as this machine's own root.
    """
    if not isinstance(sessions, list):
        return []

    refs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sessions:
        if not isinstance(item, dict):
            continue

        session_id = item.get('session_id')
        cwd = item.get('cwd')
        if not (isinstance(session_id, str) and session_id and isinstance(cwd, str) and cwd):
            continue

        origin = item.get('origin')
        if not isinstance(origin, str):
            origin = 'local'

        triple = (session_id, cwd, origin)
        if triple not in seen:
            seen.add(triple)
            refs.append(triple)

    return refs


def _resolve_roots(origins: list[str]) -> dict[str, tuple[SessionRoot, Path] | None]:
    """Resolve each distinct origin's root and its resolved ``projects/`` directory, once per call.

    An origin :func:`roots.root_for_origin` does not currently recognize - most
    often a WSL distro that has since stopped running - resolves to ``None``,
    same as one whose ``projects/`` directory cannot be resolved (an unreadable
    root); either way, every ref naming that origin is dropped by the caller.
    One origin failing to resolve never affects another's entry.
    """
    resolved: dict[str, tuple[SessionRoot, Path] | None] = {}
    for origin in origins:
        if origin in resolved:
            continue

        root = root_for_origin(origin)
        if root is None:
            resolved[origin] = None
            continue

        try:
            resolved[origin] = (root, projects_dir(root).resolve())
        except OSError:
            resolved[origin] = None

    return resolved


def _confined_transcript(session_id: str, cwd: str, root: SessionRoot, projects_root: Path) -> Path | None:
    """Resolve a session's transcript path under *root*, or ``None`` if it escapes *projects_root*.

    The path is confined the same way the deletion surface is: it is resolved
    and checked to sit under *projects_root* - that root's own resolved
    ``projects/`` directory - so a crafted id or cwd carrying path traversal can
    never point the read at a file outside that root's transcript tree, nor at
    another root's tree, since each ref is always checked against its own
    root's *projects_root*.
    """
    try:
        path = transcript_path(root, session_id, cwd).resolve()
        path.relative_to(projects_root)
    except (OSError, ValueError):
        return None

    return path if path.is_file() else None


@dataclass
class _FileScan:
    """How far one transcript has been searched for one query, and what was found.

    ``offset`` is the end of the last complete line read; a transcript only ever
    grows, so a later search for the same query resumes there.  ``tail`` holds
    the bytes just before it, which tell a file that grew from one rewritten.
    """

    offset: int = 0
    count: int = 0
    entries: int = 0
    partial: bool = False
    snippets: list[dict[str, Any]] = field(default_factory=list)
    tail: bytes = b''

    def copy(self) -> _FileScan:
        return _FileScan(self.offset, self.count, self.entries, self.partial, list(self.snippets), self.tail)

    def continues(self, handle: BinaryIO) -> bool:
        """Whether the file open in *handle* still holds, unchanged, what was read of it."""
        size = handle.seek(0, os.SEEK_END)
        if size < self.offset:
            return False

        handle.seek(self.offset - len(self.tail))
        return handle.read(len(self.tail)) == self.tail

    def result(self, session_id: str) -> dict[str, Any] | None:
        """The match to report for this transcript, or ``None`` when it holds no hit."""
        if self.count == 0:
            return None

        result: dict[str, Any] = {'session_id': session_id, 'count': self.count, 'snippets': list(self.snippets)}
        if self.partial:
            result['partial'] = True

        return result


@dataclass
class _ScanMemo:
    """The finished part of each transcript's scan for the newest query.

    An active search is repeated whenever the snapshot changes, to catch a match
    that has only just been written; without this every such repeat read each
    live transcript from the start.  Only one query is kept: a new one replaces
    it, and a cleared search drops it, so this holds at most one small record
    per transcript in view.  Process memory only, never written anywhere.
    """

    key: tuple[str, int] | None = None
    files: dict[Path, _FileScan] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_memo = _ScanMemo()


def _resume_point(path: Path, key: tuple[str, int]) -> _FileScan:
    """Return a private copy of the stored progress of *path* for *key*, or a fresh one."""
    with _memo.lock:
        if _memo.key != key:
            _memo.key = key
            _memo.files = {}
        stored = _memo.files.get(path)

    return stored.copy() if stored is not None else _FileScan()


def _forget() -> None:
    """Drop every remembered scan, excerpts included."""
    with _memo.lock:
        _memo.key = None
        _memo.files = {}


def _remember(path: Path, key: tuple[str, int], progress: _FileScan) -> None:
    """Store *progress* of *path*, unless a newer query has taken over meanwhile."""
    with _memo.lock:
        if _memo.key == key:
            _memo.files[path] = progress.copy()


def _scan_file(path: Path, session_id: str, query: _Query, should_cancel: Callable[[], bool]) -> dict[str, Any] | None:
    """Count the hits in one transcript and excerpt the first few, or ``None`` for no hit.

    The file is read in blocks of ``_CHUNK_BYTES``.  A block that does not hold
    the query's literal (see :func:`_literal`) is passed over whole; otherwise
    only the lines around each occurrence are decoded.  A line is then tested
    against the prefilter (when one could be built) before it is parsed, and a
    parsed line contributes only its readable text (see :func:`_entry_texts`);
    a line that does not parse is searched as-is.

    The scan resumes where the same query last stopped in this file, so a repeat
    reads only what was appended since.  A file that has shrunk is read afresh.
    A trailing line without its newline is still being written: it is searched,
    but not stored, so it is read again once it is complete.

    Counting stops after ``_MAX_MATCH_ENTRIES`` matching entries, which sets
    ``partial`` on the result: the count is then a floor, not a total.  Any read
    error or a cancellation yields ``None`` - a search never raises.
    """
    progress = _resume_point(path, query.key)
    if progress.partial:
        return progress.result(session_id)

    try:
        with path.open('rb') as handle:
            if not progress.continues(handle):
                progress = _FileScan()
            handle.seek(progress.offset)

            carry = b''
            while not progress.partial:
                if should_cancel():
                    return None
                block = handle.read(_CHUNK_BYTES)
                if not block:
                    break

                data = carry + block
                cut = data.rfind(b'\n') + 1
                carry = data[cut:]
                if cut and not _scan_lines(data[:cut], progress, query, should_cancel):
                    return None
                progress.offset += cut
                progress.tail = (progress.tail + data[max(0, cut - _TAIL_BYTES):cut])[-_TAIL_BYTES:]
    except OSError:
        return None

    _remember(path, query.key, progress)

    if carry and not progress.partial:
        progress = progress.copy()
        if not _scan_lines(carry, progress, query, should_cancel):
            return None

    return progress.result(session_id)


def _scan_lines(data: bytes, progress: _FileScan, query: _Query, should_cancel: Callable[[], bool]) -> bool:
    """Search a block of whole lines into *progress*; ``False`` when cancelled.

    With a literal, only the lines holding it are decoded and tested.  Without
    one - or in a block holding a character the literal's byte search would miss
    - every line is.
    """
    literal = query.literal
    haystack = data.lower() if literal is not None and literal.fold else data
    if literal is None or literal.defeated_by(haystack):
        for line in data.decode('utf-8', 'ignore').split('\n'):
            if should_cancel():
                return False
            if line and not _take_line(line, progress, query):
                break
        return True

    found = haystack.find(literal.needle)
    while found >= 0:
        if should_cancel():
            return False

        start = data.rfind(b'\n', 0, found) + 1
        end = data.find(b'\n', found)
        if end < 0:
            end = len(data)
        if not _take_line(data[start:end].decode('utf-8', 'ignore'), progress, query):
            break
        found = haystack.find(literal.needle, end + 1)

    return True


def _take_line(line: str, progress: _FileScan, query: _Query) -> bool:
    """Count one line's hits into *progress*; ``False`` once the count has reached its cap."""
    if query.prefilter is not None and not query.prefilter.search(line):
        return True
    if progress.entries >= _MAX_MATCH_ENTRIES:
        progress.partial = True
        return False

    hits = _line_hits(line, query.matcher, progress.snippets)
    if hits:
        progress.count += hits
        progress.entries += 1

    return True


def _line_hits(line: str, matcher: re.Pattern[str], snippets: list[dict[str, Any]]) -> int:
    """Count the hits in one transcript line, appending excerpts while room is left."""
    hits = 0
    for kind, tool, text in _entry_texts(line):
        for match in matcher.finditer(text):
            # A pattern that can match nothing (`a*`) would otherwise report a
            # hit between every pair of characters.
            if match.start() == match.end():
                continue

            hits += 1
            if len(snippets) < _MAX_SNIPPETS:
                snippets.append(_snippet(kind, tool, text, match))

    return hits


def _entry_texts(line: str) -> list[tuple[str, str | None, str]]:
    """Return ``(kind, tool, text)`` for each readable piece of one transcript line.

    A whitelist, not a blacklist: only the fields that carry what somebody wrote
    or read are searched - message text, thinking, tool inputs, tool results,
    a compaction summary and a system entry's own content.  The control metadata
    every other reader here confines itself to (uuids, timestamps, the project
    path repeated on every line, model ids) is left out, so a hit is always
    something worth showing.  A future field is simply not searched rather than
    silently widening what this reads.

    ``kind`` names the speaker for the interface to label - it is a token, never
    a phrase, since the wording belongs to the UI's locale.  A line that is not
    JSON at all is returned whole under the ``'raw'`` kind, so a format change
    degrades to searching the plain text instead of finding nothing.
    """
    try:
        entry = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return [('raw', None, line)]

    if not isinstance(entry, dict):
        return [('raw', None, line)]

    pieces: list[tuple[str, str | None, str]] = []

    message = entry.get('message')
    if isinstance(message, dict):
        _absorb_content(message.get('content'), _speaker(entry), pieces)

    summary = entry.get('summary')
    if isinstance(summary, str) and summary:
        pieces.append(('summary', None, summary))

    if entry.get('type') == 'system':
        content = entry.get('content')
        if isinstance(content, str) and content:
            pieces.append(('system', None, content))

    return pieces


def _speaker(entry: dict[str, Any]) -> str:
    """The kind token for an entry's own message text."""
    entry_type = entry.get('type')
    if entry_type == 'user':
        return 'user'
    if entry_type == 'assistant':
        return 'assistant'

    return 'system'


def _absorb_content(content: object, speaker: str, pieces: list[tuple[str, str | None, str]]) -> None:
    """Collect the readable text of a message's ``content``, whichever shape it has.

    Claude Code writes it either as a plain string (a simple user turn) or as a
    list of typed blocks; an unknown block type contributes nothing.
    """
    if isinstance(content, str):
        if content:
            pieces.append((speaker, None, content))
        return

    if not isinstance(content, list):
        return

    for block in content:
        if isinstance(block, str):
            if block:
                pieces.append((speaker, None, block))
            continue
        if not isinstance(block, dict):
            continue

        block_type = block.get('type')
        if block_type == 'text':
            _append_text(pieces, speaker, None, block.get('text'))
        elif block_type == 'thinking':
            _append_text(pieces, 'thinking', None, block.get('thinking'))
        elif block_type == 'tool_use':
            name = block.get('name')
            tool = name if isinstance(name, str) and name else None
            for text in _nested_strings(block.get('input')):
                pieces.append(('tool_use', tool, text))
        elif block_type == 'tool_result':
            _absorb_result(block.get('content'), pieces)


def _absorb_result(content: object, pieces: list[tuple[str, str | None, str]]) -> None:
    """Collect a tool result's own text, which is a string or a list of text blocks."""
    if isinstance(content, str):
        _append_text(pieces, 'tool_result', None, content)
        return

    if not isinstance(content, list):
        return

    for block in content:
        if isinstance(block, str):
            _append_text(pieces, 'tool_result', None, block)
        elif isinstance(block, dict) and block.get('type') == 'text':
            _append_text(pieces, 'tool_result', None, block.get('text'))


def _append_text(pieces: list[tuple[str, str | None, str]], kind: str, tool: str | None, value: object) -> None:
    """Append *value* as a searchable piece when it really is a non-empty string."""
    if isinstance(value, str) and value:
        pieces.append((kind, tool, value))


def _nested_strings(value: object, depth: int = 0) -> Iterator[str]:
    """Yield the strings inside a tool call's input, however it is structured.

    A tool's arguments are what the search is really after here - the command a
    Bash call ran, the text an Edit replaced - and their shape is the tool's own,
    so they are walked rather than named.  Depth and count are bounded so a
    pathological structure cannot turn one line into unbounded work.
    """
    if depth > _MAX_INPUT_DEPTH:
        return

    if isinstance(value, str):
        if value:
            yield value
        return

    if isinstance(value, dict):
        items: list[object] = list(value.values())
    elif isinstance(value, list):
        items = list(value)
    else:
        return

    produced = 0
    for item in items:
        for text in _nested_strings(item, depth + 1):
            yield text
            produced += 1
            if produced >= _MAX_INPUT_STRINGS:
                return


def _snippet(kind: str, tool: str | None, text: str, match: re.Match[str]) -> dict[str, Any]:
    """Build one excerpt: the hit, plus the little that fits either side of it.

    The context is cut at the hit's own line as well as at ``_CONTEXT_CHARS``,
    so a hit inside a long tool result carries its line rather than the
    paragraphs around it.  ``clipped_before``/``clipped_after`` say whether text
    was left out, for the interface to mark - the wording, here as everywhere,
    is the UI's to choose.  Both are measured against the hit's **line**, not
    the whole text: the mark sits at the excerpt's edge and reads as "this line
    goes on", so a hit at the start of its own line must not carry one merely
    because earlier lines exist.
    """
    start, end = match.start(), match.end()

    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    if line_end < 0:
        line_end = len(text)

    before_from = max(line_start, start - _CONTEXT_CHARS)
    after_to = min(line_end, end + _CONTEXT_CHARS)
    matched = text[start:end]

    snippet: dict[str, Any] = {
        'kind': kind,
        'before': _one_line(text[before_from:start]),
        'match': _one_line(matched[:_MAX_MATCH_CHARS]),
        'after': _one_line(text[end:after_to]),
    }
    if tool:
        snippet['tool'] = tool
    if before_from > line_start:
        snippet['clipped_before'] = True
    if after_to < line_end or len(matched) > _MAX_MATCH_CHARS:
        snippet['clipped_after'] = True

    return snippet


def _one_line(text: str) -> str:
    """Flatten a piece of an excerpt for a single-line display.

    Control characters are dropped and every whitespace run becomes one space,
    so an excerpt from indented code or a multi-line tool result still reads as
    one line.  Leading and trailing space is kept: it is the gap between the hit
    and its context, and removing it would run the two together.
    """
    return _WHITESPACE_RUN.sub(' ', _CONTROL_CHARS.sub('', text))
