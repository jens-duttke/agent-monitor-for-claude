"""
Transcript Metadata
===================

Reads a session transcript and extracts **only** control-flow metadata:
entry type, the last assistant turn's ``stop_reason``, tool-request/result
IDs (to detect an unanswered request), the last tool's name, whether the newest
user turn is Claude Code's interrupt marker, whether a trailing turn is an API
error (and whether that error is a usage/session limit), timestamps, the model
name, the Claude Code version that wrote each turn, aggregated token-usage
numbers, and the session title (the ``aiTitle`` Claude Code generates for its own
session list, or the ``customTitle`` the user set by renaming the session -
display metadata, not conversation content).

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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .paths import SessionRoot, transcript_path

__all__ = ['TranscriptState', 'HistoryState', 'state_for', 'history_state_for', 'prune_scan_cache']

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

# The two switch logs the scan keeps, named by the field each entry reports its
# value under.  Both are wire format - the UI reads `entry.model` / `entry.version`
# off the snapshot - and both double as the timeline cache's key.
#
# The model log answers "which model was answering, when": a model used, left,
# and returned to appears once per run, so the last entry is the current model
# with the time it was switched back to.  The version log answers the same for
# the Claude Code build that wrote each turn - a long session resumed after an
# upgrade spans several, which dates a mid-session change in behaviour.  Nothing
# assumes a version only moves forward; runs are reported as the timestamps order
# them.
_MODEL_KEY = 'model'
_CLI_VERSION_KEY = 'version'

# Fixed marker Claude Code writes as a user turn when the user interrupts a
# running turn.  On disk it is indistinguishable from a fresh prompt, yet it
# means the opposite - the model has stopped and owes nothing - so the tail
# parser flags it as its own entry kind.  Matched only to that boolean kind; the
# text is never returned or stored.  The prefix also covers the trailing
# "... for tool use" variant.
_INTERRUPT_MARKER = '[Request interrupted by user'

# Wrappers Claude Code puts around the captured output of a local command (a
# slash or ``!`` command).  Only a command that already ran outside the model
# produces them, so such a user entry is that command's execution record, never
# a prompt awaiting an answer - the same meaning as the ``system``/
# ``local_command`` entry below, in the shape newer Claude Code versions write.
# Matched only to that boolean entry kind; the output itself is never read.
_LOCAL_COMMAND_OUTPUT_MARKERS = ('<local-command-stdout>', '<local-command-stderr>')

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

# A slash command is stored as structured blocks; the name plus its arguments
# ("/work-on-issue #123") is what names the session, so both are read.
_COMMAND_NAME_PATTERN = re.compile(r'<command-name>(.*?)</command-name>', re.S)
_COMMAND_ARGS_PATTERN = re.compile(r'<command-args>(.*?)</command-args>', re.S)

# Housekeeping commands the fallback title looks past: every post-/clear
# session opens with the /clear entry itself, so it can never say what the
# session is about.  A meaningful opening command (/pr-review, a project's own
# slash command) stays the title, exactly like any other first prompt.
_HOUSEKEEPING_TITLE_COMMANDS = frozenset({'/clear'})

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
    cli_events: list[tuple[str, str]] = field(default_factory=list)
    ai_title: str | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    first_command_prompt: str | None = None
    permission_mode: str | None = None
    # Memoized run-compressed timelines, one per value field, each kept with the
    # event count it was built from.  Deliberately the last field, so the
    # positional ``copy()`` below stays correct without carrying it over - a copy
    # exists to absorb one more line, so its timelines have to be rebuilt anyway.
    timeline_cache: dict[str, tuple[int, list[dict[str, str]]]] = field(default_factory=dict)

    def title(self) -> str | None:
        # The housekeeping-command name is the last resort: anything later that
        # can actually name the session (a prompt, a meaningful command) wins.
        return self.custom_title or self.ai_title or self.first_prompt or self.first_command_prompt

    def timeline(self, value_key: str, events: list[tuple[str, str]]) -> list[dict[str, str]]:
        """Return the run-compressed timeline for *events*, rebuilt only when they have grown.

        ``_scan_result`` runs on every poll, but a transcript has usually not
        grown since the last one - and building a timeline sorts every event,
        parsing each timestamp for the sort key, which dominates the cost of an
        otherwise no-op scan (tens of milliseconds per second for a session with
        thousands of turns).  Events are only ever appended, so an unchanged
        count means an unchanged timeline.  A fresh list of fresh dicts is handed
        out, keeping the cached one unreachable from the caller.

        Parameters
        ----------
        value_key : str
            Name the value is reported under in each entry, and the cache key.
        events : list of (str, str)
            The ``(timestamp, value)`` events accumulated so far.
        """
        cached = self.timeline_cache.get(value_key)
        if cached is None or cached[0] != len(events):
            cached = (len(events), _run_timeline(events, value_key))
            self.timeline_cache[value_key] = cached

        return [dict(entry) for entry in cached[1]]

    def copy(self) -> '_ScanState':
        return _ScanState(
            self.consumed,
            dict(self.totals),
            {model: dict(usage) for model, usage in self.by_model.items()},
            list(self.model_events),
            list(self.cli_events),
            self.ai_title, self.custom_title, self.first_prompt, self.first_command_prompt, self.permission_mode,
        )


@dataclass(frozen=True)
class _ScanResult:
    """One incremental scan's plain, JSON-serializable outcome."""

    usage: dict[str, int]
    usage_by_model: dict[str, dict[str, int]]
    model_timeline: list[dict[str, str]]
    cli_timeline: list[dict[str, str]]
    title: str | None
    permission_mode: str | None


# The first poll reads the whole file once; afterwards only newly appended
# bytes are parsed (tracked per path up to the last complete line).
_scan_cache: dict[str, _ScanState] = {}

# Serializes the read-absorb-store sequence below. pywebview runs each js_api
# call on its own thread, so two overlapping snapshot builds can otherwise share
# one cached state, both absorb the same appended bytes, and double-count usage.
_scan_lock = threading.Lock()


def prune_scan_cache(active: Iterable[tuple[SessionRoot, str, str]]) -> None:
    """Drop scan-cache entries for sessions no longer in the registry.

    The cache holds one ``_ScanState`` per transcript ever scanned - per-model
    totals, the title, the full model-event list - and is otherwise never
    evicted, so a long-running monitor's memory grows with every session ever
    observed.  ``build_snapshot`` calls this each poll with the ``(root,
    session_id, cwd)`` of every current registry record across every session
    root, so only live sessions are retained.

    Parameters
    ----------
    active : iterable of (root, session_id, cwd)
        Every current registry session, paired with the root it came from; its
        cache key is computed exactly like :func:`_scan_appended` (path,
        case-normalized), so the two always agree - and including the root
        keeps two roots' transcripts apart even when a session_id/cwd pair
        happens to repeat across them.
    """
    keep = {
        os.path.normcase(str(transcript_path(root, session_id, cwd)))
        for root, session_id, cwd in active
        if session_id and cwd
    }

    with _scan_lock:
        for key in list(_scan_cache):
            if key not in keep:
                del _scan_cache[key]


@dataclass(frozen=True)
class TranscriptState:
    """Control-metadata extracted from a session transcript."""

    has_transcript: bool
    last_stop_reason: str | None = None
    pending_tool: bool = False
    last_tool_name: str | None = None
    last_timestamp: str | None = None
    last_entry_kind: str | None = None
    # Whether any line parsed into a valid entry (even one skipped for state, like
    # a sidechain turn). Drives tail-window escalation: escalate only when nothing
    # parsed (an unreadable tail), not merely when no main-conversation timestamp
    # was captured - a long sidechain-only tail parses fine and must not re-read
    # up to 16 MB every poll.
    any_parsed: bool = False
    usage_limited: bool = False
    age_seconds: float | None = None
    title: str | None = None
    model: str | None = None
    cli_version: str | None = None
    usage: dict[str, int] | None = None
    usage_by_model: dict[str, dict[str, int]] | None = None
    model_timeline: list[dict[str, str]] | None = None
    cli_timeline: list[dict[str, str]] | None = None
    permission_mode: str | None = None


@dataclass(frozen=True)
class HistoryState:
    """Display metadata for a past (non-live) session transcript."""

    session_id: str
    cwd: str | None = None
    title: str | None = None
    model: str | None = None
    cli_version: str | None = None
    age_seconds: float | None = None


def state_for(root: SessionRoot, session_id: str, cwd: str) -> TranscriptState:
    """Return the transcript state for a session under *root*, or an empty state if none exists."""
    if not session_id or not cwd:
        return TranscriptState(has_transcript=False)

    path = transcript_path(root, session_id, cwd)
    if not path.is_file():
        return TranscriptState(has_transcript=False)

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return TranscriptState(has_transcript=False)

    state = _parse(_read_tail(path))
    for window in _TAIL_ESCALATION[1:]:
        if state.any_parsed:
            break
        state = _parse(_read_tail(path, window))

    scan = _scan_appended(path)
    age_seconds = _activity_age(state.last_timestamp, mtime)
    return replace(state, age_seconds=age_seconds, usage=scan.usage, usage_by_model=scan.usage_by_model,
                   model_timeline=scan.model_timeline, cli_timeline=scan.cli_timeline,
                   title=scan.title, permission_mode=scan.permission_mode)


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
    # Escalate the tail window only when nothing parsed (an unreadable tail),
    # exactly like state_for: a single trailing entry larger than 256 KB (a giant
    # tool result) yields no complete line, leaving the model blank and the age
    # from mtime. A parseable-but-timestampless tail must not keep escalating.
    for window in _TAIL_ESCALATION[1:]:
        if tail.any_parsed:
            break
        tail = _parse(_read_tail(path, window))
    age_seconds = _activity_age(tail.last_timestamp, mtime)

    return HistoryState(session_id=session_id, cwd=cwd, title=title, model=tail.model, cli_version=tail.cli_version, age_seconds=age_seconds)


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
            text, is_housekeeping = _prompt_display_parts(entry)
            if is_housekeeping:
                if state.first_command_prompt is None:
                    state.first_command_prompt = text
            else:
                state.first_prompt = text

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


def _leading_text_starts_with(content: object, prefixes: tuple[str, ...]) -> bool:
    """Return True if a user entry's leading text opens with one of *prefixes*.

    The entry's text never leaves this function: it is compared against fixed
    control strings and only the boolean result is returned.  That is what keeps
    the two marker checks below inside the module's privacy boundary - a helper
    handing the text back would put conversation content in reach of every
    caller in the module.
    """
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                text = block.get('text')
                break

    if not isinstance(text, str):
        return False

    stripped = text.lstrip()

    return any(stripped.startswith(prefix) for prefix in prefixes)


def _is_interrupt_marker(content: object) -> bool:
    """Return True if a user entry's content is Claude Code's interrupt marker.

    Matches only the fixed control string (via prefix), and its result is
    surfaced as an entry kind - the text itself is never returned or stored.
    """
    return _leading_text_starts_with(content, (_INTERRUPT_MARKER,))


def _is_local_command_output(content: object) -> bool:
    """Return True if a user entry is the captured output of a local command.

    Newer Claude Code versions record a slash or ``!`` command's execution as an
    ordinary ``user`` entry wrapping its captured output, instead of (or besides)
    the ``system``/``local_command`` entry ``_parse`` already knows.  On disk that
    is indistinguishable from a fresh prompt, yet it means the opposite: the
    command ran outside the model, so no reply is owed.  Matched only to the
    fixed wrapper prefix and surfaced as an entry kind; the output is never
    returned or stored.
    """
    return _leading_text_starts_with(content, _LOCAL_COMMAND_OUTPUT_MARKERS)


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
    cli_version: str | None = None
    any_parsed: bool = False

    for line in lines:
        entry = _load(line)
        if entry is None:
            continue

        # A valid entry parsed - even if it is skipped for state below (sidechain
        # or isMeta). This marks the tail as readable so escalation can stop.
        any_parsed = True

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

        # The Claude Code version is stamped on every entry, not just assistant
        # turns, so the newest one of any kind is the version currently in use -
        # the counterpart of the model below, which only an assistant turn can
        # report.
        entry_version = entry.get('version')
        if isinstance(entry_version, str) and entry_version:
            cli_version = entry_version

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
            # the fixed marker Claude Code writes when the user interrupts a
            # running turn, or a local command's captured output.  Both markers
            # are plain user turns on disk but mean the opposite of a fresh
            # prompt - control is back with the user, or the command already ran
            # outside the model - so each is tracked as its own kind.
            is_interrupt = _is_interrupt_marker(content)
            is_local_command = not is_interrupt and _is_local_command_output(content)
            if is_interrupt:
                last_entry_kind = 'user_interrupt'
            elif is_local_command:
                last_entry_kind = 'local_command'
            else:
                last_entry_kind = 'user_text'
            usage_limited = False
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        # Still record the resolved id so a pending tool is
                        # cleared, but never let a tool_result downgrade either
                        # marker: the whole turn was stopped, or the entry is a
                        # command's own record, so the marker wins (matching the
                        # documented precedence).
                        if not is_interrupt and not is_local_command:
                            last_entry_kind = 'tool_result'
                        tool_use_id = block.get('tool_use_id')
                        if tool_use_id:
                            resolved_tool_ids.add(tool_use_id)

        elif entry_type == 'system' and entry.get('subtype') == 'local_command':
            # A local command (a slash or `!` command) executed. It runs outside
            # the model - Claude Code even writes a caveat telling the model not
            # to respond - so no reply is owed. Recorded as its own kind so the
            # trailing command entries are not misread as a pending prompt.
            # Claude Code writes this record for some commands and the wrapped
            # user entry above for others (`/compact` produces only the latter),
            # so both shapes have to yield the same kind.
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
        any_parsed=any_parsed,
        usage_limited=usage_limited,
        model=model,
        cli_version=cli_version,
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


def _scan_appended(path: Path) -> _ScanResult:
    """Return the incremental scan's accumulated result for *path*.

    The first call reads the whole file; subsequent calls parse only newly
    appended bytes.  Tracks summed usage (overall and per model - subagents
    often run on a cheaper model, so a valid cost needs the split), the ordered
    model-switch and CLI-version timelines of the main conversation, the display
    title, and the latest permission mode.
    """
    # Normalize case so two registry cwds that differ only in case (they resolve
    # to the same case-insensitive file) share one cache entry instead of each
    # paying a full-file scan.
    cache_key = os.path.normcase(str(path))

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

            # Only an actual partial line needs the throwaway copy. A transcript
            # normally ends on a newline, leaving nothing trailing - and reporting
            # off the cached state directly is what lets its memoized timelines
            # survive this poll, instead of being built on a copy that is
            # discarded and rebuilt on the next one.
            if trailing:
                result = state.copy()
                _absorb_line(trailing, result)

        return _scan_result(result)


def _scan_result(state: _ScanState) -> _ScanResult:
    """Snapshot a scan state into plain, JSON-serializable return values."""
    by_model = {model: dict(usage) for model, usage in state.by_model.items()}
    return _ScanResult(
        usage=dict(state.totals),
        usage_by_model=by_model,
        model_timeline=state.timeline(_MODEL_KEY, state.model_events),
        cli_timeline=state.timeline(_CLI_VERSION_KEY, state.cli_events),
        title=state.title(),
        permission_mode=state.permission_mode,
    )


def _run_timeline(events: list[tuple[str, str]], value_key: str) -> list[dict[str, str]]:
    """Sort ``(timestamp, value)`` events by time and collapse equal-value runs.

    Transcript entries are not strictly ordered on disk, so the events are sorted
    by time first, then runs of the same value are collapsed to a single entry
    carrying the moment that run began.  The result is a chronological switch
    log: one entry per *run*, so a value used, left, and returned to appears more
    than once - and the final entry is the value in use, with the time it was
    last switched to.

    Parameters
    ----------
    events : list of (str, str)
        Raw events in on-disk order, which is not necessarily chronological.
    value_key : str
        Name the value is reported under in each resulting entry.
    """
    # Sort by parsed epoch, not the raw string: lexicographic order matches
    # chronological order only while every timestamp has an identical shape, but
    # a fractional-seconds value ('...07.500Z') sorts before a whole-second one
    # ('...07Z') though it is later, and an explicit offset mis-sorts against 'Z'.
    # The raw string is kept for display; it breaks ties for equal epochs.
    timeline: list[dict[str, str]] = []
    for timestamp, value in sorted(events, key=lambda event: (_timestamp_epoch(event[0]) or 0.0, event[0])):
        if not timeline or timeline[-1][value_key] != value:
            timeline.append({'time': timestamp, value_key: value})

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

            # Record each assistant turn in the MAIN conversation as an event for
            # the two switch logs (see _MODEL_KEY / _CLI_VERSION_KEY). Ordering is
            # resolved in _run_timeline, not here, because transcript entries are
            # not strictly ordered on disk. The model excludes the synthetic
            # sentinel; the version does not.
            timestamp = entry.get('timestamp')
            main_turn = entry.get('isSidechain') is not True and isinstance(timestamp, str) and bool(timestamp)
            if main_turn and isinstance(model, str) and model and model != _SYNTHETIC_MODEL:
                state.model_events.append((timestamp, model))

            # Deliberately not gated on the synthetic sentinel like the model
            # above: that sentinel means "no real model", but a locally-generated
            # turn still comes from a real CLI version, so its version is valid
            # evidence.
            version = entry.get('version')
            if main_turn and isinstance(version, str) and version:
                state.cli_events.append((timestamp, version))

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
        text, is_housekeeping = _prompt_display_parts(entry)
        if is_housekeeping:
            if state.first_command_prompt is None:
                state.first_command_prompt = text
        else:
            state.first_prompt = text


def _prompt_display_parts(entry: dict) -> tuple[str | None, bool]:
    """Extract the display text of a prompt entry, as Claude Code's UI shows it.

    This is the one sanctioned read of prompt text (used solely as the
    fallback session title): wrapper blocks are stripped, whitespace is
    collapsed, and the result is truncated.  Entries carrying tool results
    or only wrapper content yield ``(None, False)``.

    Returns
    -------
    tuple[str or None, bool]
        The display text, and whether that text is a housekeeping command
        (``_HOUSEKEEPING_TITLE_COMMANDS``) the title fallback should look
        past when a later prompt can name the session better.
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
                return None, False
            if text is None and block.get('type') == 'text':
                text = block.get('text')

    if not isinstance(text, str):
        return None, False

    # A slash-command prompt: its name plus arguments, clipped like any title.
    command_match = _COMMAND_NAME_PATTERN.search(text)
    if command_match:
        command_name = command_match.group(1).strip()
        if command_name:
            # Collapsed, not just edge-stripped: the args block matches across
            # newlines (re.S), and a raw newline must never reach a title.
            args_match = _COMMAND_ARGS_PATTERN.search(text)
            args = ' '.join(args_match.group(1).split()) if args_match else ''
            display = f'{command_name} {args}' if args else command_name
            if len(display) > _TITLE_MAX_CHARS:
                display = display[:_TITLE_MAX_CHARS - 1] + '…'
            return display, command_name in _HOUSEKEEPING_TITLE_COMMANDS

    cleaned = ' '.join(_WRAPPER_PATTERN.sub('', text).split())
    if not cleaned:
        return None, False

    if len(cleaned) > _TITLE_MAX_CHARS:
        cleaned = cleaned[:_TITLE_MAX_CHARS - 1] + '…'

    return cleaned, False


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
