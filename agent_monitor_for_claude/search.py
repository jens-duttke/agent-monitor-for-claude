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
so it never hides a hit that the readable text would have produced.

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
- Files are scanned concurrently on a thread pool, and matches are emitted
  strictly in newest-first order.
- ``should_cancel`` is polled throughout (per file and per line), so a superseded
  search - the user typed another character - stops promptly.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from .paths import SessionRoot, projects_dir, transcript_path
from .roots import root_for_origin

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

    matcher, prefilter, invalid = _compile_matcher(query, options)
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

    def check(item: tuple[Path, str]) -> dict[str, Any] | None:
        path, session_id = item
        if cancel():
            return None
        return _scan_file(path, session_id, matcher, prefilter, cancel)

    workers = min(_MAX_WORKERS, max(1, (os.cpu_count() or 4) * 4), total)
    processed = 0
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map yields in submission order, so matches are emitted strictly
        # newest-file-first even though the scans run concurrently.
        for result in pool.map(check, ordered):
            if cancel():
                return

            processed += 1
            if result is not None and result['session_id'] not in seen:
                seen.add(result['session_id'])
                pending.append(result)

            if len(pending) >= _BATCH_SIZE or processed % _PROGRESS_EVERY == 0:
                on_update(processed, total, pending, False, False)
                pending = []

    if not cancel():
        on_update(processed, total, pending, True, False)


def _never() -> bool:
    return False


def _compile_matcher(query: object, options: object) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None, bool]:
    """Compile the query + options into a matcher and its raw-line prefilter.

    Returns ``(matcher, prefilter, invalid)``: a compiled pattern when the query
    is usable, ``(None, None, False)`` when it is blank or over-long (no filter,
    not an error), or ``(None, None, True)`` when regular-expression mode was on
    and the pattern would not compile (an error the UI flags).  The prefilter is
    ``None`` when no sound one can be built, in which case every line is parsed.
    """
    if not isinstance(query, str):
        return None, None, False

    text = query.strip()
    if not text or len(text) > _MAX_QUERY_LEN:
        return None, None, False

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
        return None, None, True

    return matcher, _prefilter(text, use_regex, match_case), False


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
        return re.escape(char)

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


def _scan_file(
    path: Path,
    session_id: str,
    matcher: re.Pattern[str],
    prefilter: re.Pattern[str] | None,
    should_cancel: Callable[[], bool],
) -> dict[str, Any] | None:
    """Count the hits in one transcript and excerpt the first few, or ``None`` for no hit.

    Each line is tested against *prefilter* (when one could be built) before it
    is parsed, so a line that cannot match costs one regex test and nothing more.  A parsed line contributes only its readable text (see
    :func:`_entry_texts`); a line that does not parse is searched as-is.

    Counting stops after ``_MAX_MATCH_ENTRIES`` matching entries, which sets
    ``partial`` on the result: the count is then a floor, not a total.  Any read
    error yields ``None`` - a search never raises.
    """
    count = 0
    snippets: list[dict[str, Any]] = []
    entries = 0
    partial = False

    try:
        with path.open('r', encoding='utf-8', errors='ignore') as handle:
            for line in handle:
                if should_cancel():
                    return None
                if prefilter is not None and not prefilter.search(line):
                    continue
                if entries >= _MAX_MATCH_ENTRIES:
                    partial = True
                    break

                hits = _line_hits(line, matcher, snippets)
                if hits:
                    count += hits
                    entries += 1
    except OSError:
        return None

    if count == 0:
        return None

    result: dict[str, Any] = {'session_id': session_id, 'count': count, 'snippets': snippets}
    if partial:
        result['partial'] = True

    return result


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
