"""
Transcript Metadata
===================

Reads a session transcript and extracts **only** control-flow metadata:
entry type, the last assistant turn's ``stop_reason``, tool-request/result
IDs (to detect an unanswered request), the last tool's name, whether the newest
user turn is Claude Code's interrupt marker, whether a trailing turn is an API
error (and whether that error is a usage/session limit), timestamps, the model
name, aggregated token-usage numbers, and the session title (the ``aiTitle`` Claude
Code generates for its own session list, or the ``customTitle`` the user set by
renaming the session - display metadata, not conversation content).

This module is the privacy boundary of the application.  It must never read,
return, store, or expose conversation content - message ``text``, ``thinking``
blocks, tool ``input``, or tool-result ``content`` are never accessed.  The
narrow exceptions yield only metadata, never the surrounding content: the
sanctioned title read above, and matching the fixed interrupt-marker string to
a boolean entry kind.  A dedicated test enforces this.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .paths import transcript_path

__all__ = ['TranscriptState', 'HistoryState', 'state_for', 'history_state_for']

# Bytes read from the end of the transcript.  Large enough to contain the last
# several turns without loading a multi-megabyte file on every poll.  When a
# single huge entry (e.g. a giant tool result) fills the window and nothing
# parses, the window escalates so the state never goes blind.
_TAIL_BYTES = 262144
_TAIL_ESCALATION = (_TAIL_BYTES, _TAIL_BYTES * 8, _TAIL_BYTES * 64)

_USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')

# Cache-creation tokens split by TTL, nested under ``usage['cache_creation']``.
# The 5m and 1h writes are priced differently, so they are tracked separately
# in addition to the combined ``cache_creation_input_tokens`` total above.
_CACHE_TTL_KEYS = {
    'ephemeral_5m_input_tokens': 'cache_creation_5m_input_tokens',
    'ephemeral_1h_input_tokens': 'cache_creation_1h_input_tokens',
}
_USAGE_TOTAL_KEYS = _USAGE_KEYS + tuple(_CACHE_TTL_KEYS.values())

# Claude Code writes locally-generated assistant turns (interrupts, injected
# notices, etc.) with this sentinel as the model name and zero usage.  It is not
# a real model, so it must not appear in the per-model split or the model-switch
# history.
_SYNTHETIC_MODEL = '<synthetic>'

# Fixed marker Claude Code writes as a user turn when the user interrupts a
# running turn.  On disk it is indistinguishable from a fresh prompt, yet it
# means the opposite - the model has stopped and owes nothing - so the tail
# parser flags it as its own entry kind.  Matched only to that boolean kind; the
# text is never returned or stored.  The prefix also covers the trailing
# "... for tool use" variant.
_INTERRUPT_MARKER = '[Request interrupted by user'

_USAGE_MARKER = b'"usage"'
_AI_TITLE_MARKER = b'ai-title'
_CUSTOM_TITLE_MARKER = b'custom-title'
_PERMISSION_MODE_MARKER = b'permission-mode'
_CWD_MARKER = b'"cwd"'
_USER_MARKER = b'"user"'

# Display length cap for the first-prompt fallback title.
_TITLE_MAX_CHARS = 80

# Wrapper blocks Claude Code injects around prompts; stripped before using a
# prompt as the fallback title, mirroring what Claude Code's own UI displays.
_WRAPPER_TAGS = (
    'local-command-caveat', 'local-command-stdout', 'local-command-stderr',
    'system-reminder', 'ide_opened_file', 'ide_selection', 'ide_diagnostics',
    'command-name', 'command-message', 'command-args', 'command-contents',
)
_WRAPPER_PATTERN = re.compile('|'.join(f'<{tag}>.*?</{tag}>' for tag in _WRAPPER_TAGS), re.S)

# A slash command is stored as a structured block; Claude Code's tab shows
# just the command name from it.
_COMMAND_NAME_PATTERN = re.compile(r'<command-name>(.*?)</command-name>', re.S)

@dataclass
class _ScanState:
    """Accumulated state of a full incremental scan of one transcript.

    Titles and the permission mode can sit megabytes before the tail window,
    so they are tracked here rather than in the tail parser.  ``title()``
    mirrors Claude Code's precedence: a manual rename, then the auto-generated
    title, then the first prompt.
    """

    consumed: int = 0
    totals: dict[str, int] = field(default_factory=lambda: {key: 0 for key in _USAGE_TOTAL_KEYS})
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    model_events: list[tuple[str, str]] = field(default_factory=list)
    ai_title: str | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    permission_mode: str | None = None

    def title(self) -> str | None:
        return self.custom_title or self.ai_title or self.first_prompt

    def copy(self) -> '_ScanState':
        return _ScanState(
            self.consumed,
            dict(self.totals),
            {model: dict(usage) for model, usage in self.by_model.items()},
            list(self.model_events),
            self.ai_title, self.custom_title, self.first_prompt, self.permission_mode,
        )


# The first poll reads the whole file once; afterwards only newly appended
# bytes are parsed (tracked per path up to the last complete line).
_scan_cache: dict[str, _ScanState] = {}

# Serializes the read-absorb-store sequence below. pywebview runs each js_api
# call on its own thread, so two overlapping snapshot builds can otherwise share
# one cached state, both absorb the same appended bytes, and double-count usage.
_scan_lock = threading.Lock()


@dataclass(frozen=True)
class TranscriptState:
    """Control-metadata extracted from a session transcript."""

    has_transcript: bool
    last_stop_reason: str | None = None
    pending_tool: bool = False
    last_tool_name: str | None = None
    last_timestamp: str | None = None
    last_entry_kind: str | None = None
    usage_limited: bool = False
    age_seconds: float | None = None
    title: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    usage_by_model: dict[str, dict[str, int]] | None = None
    model_timeline: list[dict[str, str]] | None = None
    permission_mode: str | None = None


@dataclass(frozen=True)
class HistoryState:
    """Display metadata for a past (non-live) session transcript."""

    session_id: str
    cwd: str | None = None
    title: str | None = None
    model: str | None = None
    age_seconds: float | None = None


def state_for(session_id: str, cwd: str) -> TranscriptState:
    """Return the transcript state for a session, or an empty state if none exists."""
    if not session_id or not cwd:
        return TranscriptState(has_transcript=False)

    path = transcript_path(session_id, cwd)
    if not path.is_file():
        return TranscriptState(has_transcript=False)

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return TranscriptState(has_transcript=False)

    state = _parse(_read_tail(path))
    for window in _TAIL_ESCALATION[1:]:
        if state.last_timestamp is not None:
            break
        state = _parse(_read_tail(path, window))

    usage, usage_by_model, model_timeline, title, permission_mode = _scan_appended(path)
    age_seconds = _activity_age(state.last_timestamp, mtime)
    return replace(state, age_seconds=age_seconds, usage=usage, usage_by_model=usage_by_model,
                   model_timeline=model_timeline, title=title, permission_mode=permission_mode)


def history_state_for(path: Path) -> HistoryState:
    """Return display metadata for a past session transcript, keyed by its file.

    Unlike :func:`state_for` - which begins from a live registry record and
    needs only the tail plus an incremental usage scan - a history entry is
    discovered by walking ``projects/`` and has no registry record, so its
    ``cwd`` (used to group the session under its project) is unknown up front
    and must be recovered from the transcript itself.

    The correct title can sit anywhere in the file (a late rename writes its
    entry at that point, not at the head), so the whole file is read once - but
    only the few title-bearing lines, the first prompt, and the first ``cwd``
    are parsed; the usage of every turn is skipped, which keeps the scan roughly
    twice as fast as a full :func:`state_for`.  The current model and the
    activity age come from a cheap tail read.

    Parameters
    ----------
    path : Path
        The session-level transcript file (``projects/<slug>/<session>.jsonl``).
    """
    session_id = path.stem
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return HistoryState(session_id=session_id)

    title, cwd = _scan_title_cwd(path)
    tail = _parse(_read_tail(path))
    age_seconds = _activity_age(tail.last_timestamp, mtime)

    return HistoryState(session_id=session_id, cwd=cwd, title=title, model=tail.model, age_seconds=age_seconds)


def _scan_title_cwd(path: Path) -> tuple[str | None, str | None]:
    """Read a transcript once, resolving the correct title and the session cwd.

    Mirrors Claude Code's title precedence (a manual rename, then the
    auto-generated title, then the first prompt) by scanning the whole file, but
    parses only title-bearing lines, the first user prompt, and the first entry
    carrying a ``cwd``.  Usage-bearing turns are skipped, so a history listing
    never pays the full usage-aggregation cost.  No conversation content is
    read: only the sanctioned title fields (mirroring Claude Code's own UI) and
    the working directory.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, None

    state = _ScanState()
    cwd: str | None = None

    for raw_line in data.split(b'\n'):
        is_title = _AI_TITLE_MARKER in raw_line or _CUSTOM_TITLE_MARKER in raw_line
        need_prompt = state.first_prompt is None and _USER_MARKER in raw_line
        need_cwd = cwd is None and _CWD_MARKER in raw_line
        if not (is_title or need_prompt or need_cwd):
            continue

        entry = _load(raw_line.decode('utf-8', errors='ignore'))
        if entry is None:
            continue

        if need_cwd:
            value = entry.get('cwd')
            if isinstance(value, str) and value:
                cwd = value

        entry_type = entry.get('type')
        if entry_type == 'ai-title':
            value = entry.get('aiTitle')
            if isinstance(value, str) and value:
                state.ai_title = value
        elif entry_type == 'custom-title':
            value = entry.get('customTitle')
            if isinstance(value, str) and value:
                state.custom_title = value
        elif (entry_type == 'user' and state.first_prompt is None
                and entry.get('isSidechain') is not True and entry.get('isMeta') is not True):
            # Skip injected isMeta entries (a continuation summary) here too, so a
            # history row's title is the first real prompt, not the machine digest.
            state.first_prompt = _prompt_display_text(entry)

    return state.title(), cwd


def _activity_age(last_timestamp: str | None, mtime: float) -> float:
    """Return seconds since the session's last transcript activity.

    Prefers the newest entry's timestamp, so an idle process that rewrites
    session metadata in place - bumping the file mtime without appending a
    turn - does not reset the age.  Falls back to the file mtime only when no
    entry carries a parseable timestamp.
    """
    epoch = _timestamp_epoch(last_timestamp)
    if epoch is None:
        epoch = mtime

    return max(0.0, time.time() - epoch)


def _timestamp_epoch(timestamp: str | None) -> float | None:
    """Convert an ISO-8601 transcript timestamp to POSIX seconds, or None.

    Claude Code records timestamps in UTC with a trailing ``Z``; that suffix
    is normalized to an explicit offset, and a value without any offset is
    read as UTC, so the result is directly comparable to ``time.time()``.
    """
    if not isinstance(timestamp, str) or not timestamp:
        return None

    text = timestamp.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.timestamp()


def _read_tail(path: Path, max_bytes: int = _TAIL_BYTES) -> list[str]:
    """Return the last lines of *path*, dropping a leading partial line."""
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []

    # Split on the record delimiter only, exactly like the incremental scanner
    # (``_scan_appended``): str.splitlines() also breaks on U+0085/U+2028 and
    # other Unicode boundaries a JSON value may legitimately contain, which would
    # shred that entry into unparseable fragments and lose the newest state.
    lines = data.decode('utf-8', errors='ignore').split('\n')
    if start > 0 and lines:
        return lines[1:]

    return lines


def _is_interrupt_marker(content: object) -> bool:
    """Return True if a user entry's content is Claude Code's interrupt marker.

    Matches only the fixed control string (via prefix), and its result is
    surfaced as an entry kind - the text itself is never returned or stored.
    """
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                text = block.get('text')
                break

    return isinstance(text, str) and text.lstrip().startswith(_INTERRUPT_MARKER)


def _parse(lines: list[str]) -> TranscriptState:
    """Extract control-metadata from transcript lines.

    Only structural keys are read; no conversation content is ever accessed.
    """
    resolved_tool_ids: set[str] = set()
    last_tool_id: str | None = None
    last_tool_name: str | None = None
    last_stop_reason: str | None = None
    last_timestamp: str | None = None
    last_entry_kind: str | None = None
    usage_limited: bool = False
    model: str | None = None

    for line in lines:
        entry = _load(line)
        if entry is None:
            continue

        # Sidechain entries belong to embedded subagent conversations; their
        # turns and tool calls must not drive the main conversation's state.
        if entry.get('isSidechain') is True:
            continue

        # Notices Claude Code injects into the conversation (the local-command
        # "DO NOT respond" caveat, continuation summaries, ...) carry isMeta.
        # They are not conversational turns, so - like sidechain entries - they
        # must never be read as a prompt the model owes a response to.
        if entry.get('isMeta') is True:
            continue

        timestamp = entry.get('timestamp')
        if isinstance(timestamp, str):
            last_timestamp = timestamp

        entry_type = entry.get('type')
        message = entry.get('message')
        content = message.get('content') if isinstance(message, dict) else None

        if entry_type == 'assistant' and isinstance(message, dict):
            if entry.get('isApiErrorMessage') is True:
                # A locally-generated error turn (a usage/session limit, an
                # overload, or a server error). The turn stopped and nothing is
                # running, so it is its own kind - never the pending assistant
                # turn that a non-end_turn stop_reason would otherwise imply and
                # read as "working". Only the structural error fields are read
                # (status/kind), never the message text.
                last_entry_kind = 'api_error'
                last_stop_reason = message.get('stop_reason')
                usage_limited = _is_usage_limit(entry)
            else:
                last_entry_kind = 'assistant'
                # A real turn superseded any earlier API error, so usage_limited
                # (set only in the api_error branch) must not linger True - it
                # reflects the trailing entry alone.
                usage_limited = False
                last_stop_reason = message.get('stop_reason')
                entry_model = message.get('model')
                # Keep the last *real* model for the column; the synthetic sentinel
                # (locally-generated turns) is not a model and must not be displayed.
                if isinstance(entry_model, str) and entry_model and entry_model != _SYNTHETIC_MODEL:
                    model = entry_model
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'tool_use':
                            last_tool_id = block.get('id')
                            last_tool_name = block.get('name')

        elif entry_type == 'user':
            # A user entry is a fresh prompt, a tool_result answering a request,
            # or the fixed marker Claude Code writes when the user interrupts a
            # running turn.  The interrupt marker is a plain user turn on disk
            # but means the opposite of a fresh prompt - control is back with the
            # user and the model owes nothing - so it is tracked as its own kind.
            is_interrupt = _is_interrupt_marker(content)
            last_entry_kind = 'user_interrupt' if is_interrupt else 'user_text'
            usage_limited = False
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        # Still record the resolved id so a pending tool is
                        # cleared, but never let a tool_result downgrade the
                        # interrupt marker: the whole turn was stopped, so the
                        # interrupt wins (matching the documented precedence).
                        if not is_interrupt:
                            last_entry_kind = 'tool_result'
                        tool_use_id = block.get('tool_use_id')
                        if tool_use_id:
                            resolved_tool_ids.add(tool_use_id)

        elif entry_type == 'system' and entry.get('subtype') == 'local_command':
            # A local command (a slash or `!` command) executed. It runs outside
            # the model - Claude Code even writes a caveat telling the model not
            # to respond - so no reply is owed. Recorded as its own kind so the
            # trailing command entries are not misread as a pending prompt.
            last_entry_kind = 'local_command'
            usage_limited = False

    pending_tool = last_tool_id is not None and last_tool_id not in resolved_tool_ids

    return TranscriptState(
        has_transcript=True,
        last_stop_reason=last_stop_reason,
        pending_tool=pending_tool,
        last_tool_name=last_tool_name,
        last_timestamp=last_timestamp,
        last_entry_kind=last_entry_kind,
        usage_limited=usage_limited,
        model=model,
    )


def _is_usage_limit(entry: dict) -> bool:
    """Return True if an API-error entry is a usage/session limit (HTTP 429).

    Distinguishes the rate-limit case (the model cannot continue until the
    limit resets) from other API errors, so the UI can name it precisely.
    Both the numeric status and the ``error`` token are checked defensively.
    """
    status = entry.get('apiErrorStatus')
    if status == 429 or status == '429':
        return True

    return entry.get('error') == 'rate_limit'


def _scan_appended(path: Path) -> tuple[dict[str, int], dict[str, dict[str, int]], list[dict[str, str]], str | None, str | None]:
    """Return (token totals, per-model totals, model timeline, title, permission mode), reading incrementally.

    The first call reads the whole file; subsequent calls parse only newly
    appended bytes.  Tracks summed usage (overall and per model - subagents
    often run on a cheaper model, so a valid cost needs the split), the ordered
    model-switch timeline of the main conversation (for the model-switch
    history), the display title, and the latest permission mode.
    """
    cache_key = str(path)

    with _scan_lock:
        state = _scan_cache.get(cache_key) or _ScanState()

        try:
            size = path.stat().st_size
        except OSError:
            return _scan_result(state)

        if size < state.consumed:
            state = _ScanState()

        result = state

        if size > state.consumed:
            try:
                with path.open('rb') as handle:
                    handle.seek(state.consumed)
                    data = handle.read(size - state.consumed)
            except OSError:
                return _scan_result(state)

            lines = data.split(b'\n')
            # The final chunk may be a line still being written (or a file without
            # a trailing newline): reflect it in the result, but keep it out of the
            # cache so it is re-read - never double-counted - on the next poll.
            trailing = lines.pop()
            for raw_line in lines:
                _absorb_line(raw_line, state)
            state.consumed = size - len(trailing)
            _scan_cache[cache_key] = state

            result = state.copy()
            _absorb_line(trailing, result)

        return _scan_result(result)


def _scan_result(state: _ScanState) -> tuple[dict[str, int], dict[str, dict[str, int]], list[dict[str, str]], str | None, str | None]:
    """Snapshot a scan state into plain, JSON-serializable return values."""
    by_model = {model: dict(usage) for model, usage in state.by_model.items()}
    return dict(state.totals), by_model, _model_timeline(state.model_events), state.title(), state.permission_mode


def _model_timeline(events: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Compress the main-conversation model events into an ordered switch log.

    Transcript entries are not strictly ordered on disk, so the raw ``(timestamp,
    model)`` events are sorted by time first, then runs of the same model are
    collapsed to a single entry carrying the moment that run began.  The result
    is the chronological switch history: one entry per model *run*, so a model
    used, left, and returned to appears more than once - the final entry is the
    currently active model with the time it was last switched to.
    """
    # Sort by parsed epoch, not the raw string: lexicographic order matches
    # chronological order only while every timestamp has an identical shape, but
    # a fractional-seconds value ('...07.500Z') sorts before a whole-second one
    # ('...07Z') though it is later, and an explicit offset mis-sorts against 'Z'.
    # The raw string is kept for display; it breaks ties for equal epochs.
    timeline: list[dict[str, str]] = []
    for timestamp, model in sorted(events, key=lambda event: (_timestamp_epoch(event[0]) or 0.0, event[0])):
        if not timeline or timeline[-1]['model'] != model:
            timeline.append({'time': timestamp, 'model': model})

    return timeline


def _add_usage(totals: dict[str, int], key: str, value: object) -> None:
    """Add a positive integer usage *value* to ``totals[key]``, ignoring anything else."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        totals[key] += value


def _absorb_line(raw_line: bytes, state: _ScanState) -> None:
    """Fold one transcript line into the scan state (usage, title, mode)."""
    # Marker pre-filtering skips irrelevant lines cheaply - except while the
    # first prompt is still unknown, when user entries must be inspected too.
    interesting = (
        _USAGE_MARKER in raw_line
        or _AI_TITLE_MARKER in raw_line
        or _CUSTOM_TITLE_MARKER in raw_line
        or _PERMISSION_MODE_MARKER in raw_line
    )
    if not interesting and (state.first_prompt is not None or b'"user"' not in raw_line):
        return

    entry = _load(raw_line.decode('utf-8', errors='ignore'))
    if entry is None:
        return

    entry_type = entry.get('type')

    if entry_type == 'assistant':
        message = entry.get('message')
        usage = message.get('usage') if isinstance(message, dict) else None
        if isinstance(usage, dict):
            # Sum into the overall totals always, and into a per-model bucket for
            # real models only. The synthetic sentinel (locally-generated turns,
            # zero usage) is not a real model - bucketing it would put an
            # unpriceable key in usage_by_model and wrongly drop the whole
            # session's cost to a plain token total.
            model = message.get('model') if isinstance(message, dict) else None
            bucket = None
            if model != _SYNTHETIC_MODEL:
                model_key = model if isinstance(model, str) and model else ''
                bucket = state.by_model.setdefault(model_key, {key: 0 for key in _USAGE_TOTAL_KEYS})

            for key in _USAGE_KEYS:
                value = usage.get(key)
                _add_usage(state.totals, key, value)
                if bucket is not None:
                    _add_usage(bucket, key, value)

            creation = usage.get('cache_creation')
            if isinstance(creation, dict):
                for nested_key, total_key in _CACHE_TTL_KEYS.items():
                    value = creation.get(nested_key)
                    _add_usage(state.totals, total_key, value)
                    if bucket is not None:
                        _add_usage(bucket, total_key, value)

            # Record each real assistant turn in the MAIN conversation
            # (sidechain/subagent turns and the synthetic sentinel excluded) as a
            # (timestamp, model) event. These feed the model-switch timeline,
            # which is sorted and run-compressed in _model_timeline - order is
            # resolved there because transcript entries are not strictly ordered.
            timestamp = entry.get('timestamp')
            if (isinstance(model, str) and model and model != _SYNTHETIC_MODEL and entry.get('isSidechain') is not True
                    and isinstance(timestamp, str) and timestamp):
                state.model_events.append((timestamp, model))

    elif entry_type == 'ai-title':
        value = entry.get('aiTitle')
        if isinstance(value, str) and value:
            state.ai_title = value

    elif entry_type == 'custom-title':
        value = entry.get('customTitle')
        if isinstance(value, str) and value:
            state.custom_title = value

    elif entry_type == 'permission-mode':
        value = entry.get('permissionMode')
        if isinstance(value, str) and value:
            state.permission_mode = value

    elif (entry_type == 'user' and state.first_prompt is None
            and entry.get('isSidechain') is not True and entry.get('isMeta') is not True):
        # Skip injected isMeta entries (a continuation summary), mirroring _parse:
        # the machine digest must not become the title, and it is a wider read
        # than the title path intends.
        state.first_prompt = _prompt_display_text(entry)


def _prompt_display_text(entry: dict) -> str | None:
    """Extract the display text of a prompt entry, as Claude Code's UI shows it.

    This is the one sanctioned read of prompt text (used solely as the
    fallback session title): wrapper blocks are stripped, whitespace is
    collapsed, and the result is truncated.  Entries carrying tool results
    or only wrapper content yield None.
    """
    message = entry.get('message')
    content = message.get('content') if isinstance(message, dict) else None

    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_result':
                return None
            if text is None and block.get('type') == 'text':
                text = block.get('text')

    if not isinstance(text, str):
        return None

    # A slash-command prompt: show only its name, exactly like Claude Code.
    command_match = _COMMAND_NAME_PATTERN.search(text)
    if command_match:
        command_name = command_match.group(1).strip()
        if command_name:
            return command_name

    cleaned = ' '.join(_WRAPPER_PATTERN.sub('', text).split())
    if not cleaned:
        return None

    if len(cleaned) > _TITLE_MAX_CHARS:
        cleaned = cleaned[:_TITLE_MAX_CHARS - 1] + '…'

    return cleaned


def _load(line: str) -> dict | None:
    """Parse one JSONL line into a dict, or return None on any error."""
    line = line.strip()
    if not line:
        return None

    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    return value if isinstance(value, dict) else None
