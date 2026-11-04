"""
Transcript Metadata
===================

Reads a session transcript and extracts **only** control-flow metadata:
entry type, the last assistant turn's ``stop_reason``, tool-request/result
IDs (to detect an unanswered request), the last tool's name, whether the newest
user turn is Claude Code's interrupt marker, whether a trailing turn is an API
error (and whether that error is a usage/session limit), timestamps, the model
name, the Claude Code version that wrote each turn, aggregated token-usage
numbers, the session title (the ``aiTitle`` Claude Code generates for its own
session list, or the ``customTitle`` the user set by renaming the session -
display metadata, not conversation content), the worktree the session
switched into (directory paths and a branch name from Claude Code's own
``worktree-state`` entry), and the control values that refine a status label:
a usage limit's type and reset time, a retried request's attempt counters, a
``/loop`` wakeup's delay, the two flags of Claude Desktop's turn-ending call,
and the one-word label of what opened a turn.  Beside the status it reads the
session details the row shows: the git branch, a reply's effort level and
speed and the size of its context, a dollar budget, queue operations, the
step statuses of Claude Desktop's plan, failing hooks' names and exit codes,
the artifact count, linked pull requests, and how often the context was
compacted.  For the cost tooltip it reads the name of the skill a turn ran
under (``attributionSkill``) and a web search's count of searches made
(``toolUseResult.searchCount``); the same usage scan also sums the transcripts
of a session's subagents (:func:`usage_scan`).

This module is the privacy boundary of the application.  It must never read,
return, store, or expose conversation content - message ``text``, ``thinking``
blocks, tool ``input``, or tool-result ``content`` are never accessed.  The
narrow exceptions yield only metadata, never the surrounding content: the
sanctioned title read above, matching the fixed interrupt-marker string to a
boolean entry kind, the first line of the CLI's own wording of an API error
or of a request it is retrying, and the file path of a file-writing tool call,
which is only counted.  A dedicated test enforces this.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from .background_scans import drop_pending, queue_scan, wait_for_readers
from .paths import SessionRoot, transcript_path
from .usage_parts import UsageScan

__all__ = [
    'ApiRetry', 'HookError', 'PullRequest', 'TranscriptState', 'HistoryState', 'WorktreeState', 'state_for',
    'history_state_for', 'worktree_for', 'prune_scan_cache', 'usage_scan', 'usage_scan_once',
]

# Bytes read from the end of the transcript.  Large enough to contain the last
# several turns without loading a multi-megabyte file on every poll.  When a
# single huge entry (e.g. a giant tool result) fills the window and nothing
# parses, the window escalates so the state never goes blind.
_TAIL_BYTES = 262144
_TAIL_ESCALATION = (_TAIL_BYTES, _TAIL_BYTES * 8, _TAIL_BYTES * 64)

# A transcript's first full scan reads the whole file, at roughly 400 MB/s -
# seconds for the few-hundred-megabyte transcripts long sessions grow into.  Up
# to this size it runs on the request that needs it; anything larger is handed
# to a background thread (see _scan_or_defer), so a snapshot never waits on it.
_SYNC_SCAN_BYTES = 8 * 1024 * 1024
# Read size of a full scan, which bounds its memory to a chunk plus the longest
# line instead of the whole file, and how far a background scan runs before it
# checks whether to give way (a few milliseconds of work).
_SCAN_CHUNK_BYTES = 1024 * 1024
# How far back from the end the stand-in for a running background scan looks
# for the title and permission mode (see _recent_title_and_mode), and in what
# steps.  Claude Code rewrites the title every few turns; the mode can sit a few
# megabytes back, behind a long run of tool results.
_PENDING_LOOKBACK_BYTES = 16 * 1024 * 1024
_LOOKBACK_CHUNK_BYTES = 1024 * 1024

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

# The two switch logs the scan keeps, named by the fields each entry reports its
# values under.  All three names are wire format - the UI reads `entry.model`,
# `entry.version` and `entry.entrypoint` off the snapshot - and each log's key
# tuple doubles as the timeline cache's key.
#
# The model log answers "which model was answering, when": a model used, left,
# and returned to appears once per run, so the last entry is the current model
# with the time it was switched back to.  The version log answers the same for
# the Claude Code build that wrote each turn - a long session resumed after an
# upgrade spans several, which dates a mid-session change in behaviour.  Nothing
# assumes a version only moves forward; runs are reported as the timestamps order
# them.  The version log carries a second value, the entrypoint the writing
# process stamped on the entry (``cli``, ``claude-vscode``), and a run is a
# stretch where *both* stay the same: two Claude Code processes can hold one
# session at once - a VS Code window and a terminal that resumed it, observed on
# different versions - and their interleaved turns read as the version flipping
# back and forth every few seconds, which only the writer beside each run
# explains.  An entry without an entrypoint stores '' and reports none.
_MODEL_KEY = 'model'
_CLI_VERSION_KEY = 'version'
_CLI_ENTRYPOINT_KEY = 'entrypoint'
_MODEL_KEYS = (_MODEL_KEY,)
_CLI_KEYS = (_CLI_VERSION_KEY, _CLI_ENTRYPOINT_KEY)

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

# The tool a self-paced ``/loop`` schedules its next run with.  Its call carries
# the delay as a number (and ``stop`` to end the loop), which is all that is read:
# a turn that ended after scheduling one is not waiting for you - the session
# wakes itself.  The prompt and reason it carries stay unread.
_WAKEUP_TOOL = 'ScheduleWakeup'

# Claude Desktop's own turn-ending tool.  Its call says whether the agent ends
# the turn with a question for you (``ask``) or cannot continue without your
# decision (``blocked``).  Only those two are read, and only as flags - the
# recap, the question and the suggested actions are text and stay unread.
_WRAP_UP_TOOL = 'mcp__ccd_turn__wrap_up'

# Claude Desktop's progress list for the running turn: a call either sets the
# whole list (``items``), moves on by one step (``advance``), or marks steps done
# and names the one now in progress (``done``/``start``, 1-based).  Only each
# item's status is read; the step texts stay unread.
_PLAN_TOOL = 'mcp__ccd_turn__update_plan'
_PLAN_COMPLETED = 'completed'
_PLAN_IN_PROGRESS = 'in_progress'
_PLAN_PENDING = 'pending'

# The tools that write a file, each with the input field naming that file.  The
# path is read to count distinct files only - it is neither kept past the scan
# nor reported - and nothing else of the call (the text written) is touched.
_FILE_EDIT_TOOLS = {'Edit': 'file_path', 'MultiEdit': 'file_path', 'Write': 'file_path', 'NotebookEdit': 'notebook_path'}

# The skill a turn ran under, as Claude Code stamps it on every reply that
# turn produced (``review``, ``next:review-fix``) - a subagent's replies carry
# the skill that started it.  A name of any other shape is not attributed.
_SKILL_NAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,63}')

# A web search's result records how many searches it made; each is billed on
# top of the tokens.  Only that count is read - the query and the results stay
# unread.
_SEARCH_COUNT_MARKER = b'"searchCount"'

# A short lowercase token Claude Code writes as an effort level or a speed
# (``high``, ``xhigh``, ``fast``); anything else is not displayed.
_LABEL_TOKEN_PATTERN = re.compile(r'^[a-z][a-z0-9_-]{0,15}$')

# Caps on what the tail reports for display: the newest pull requests, the most
# frequent failing hooks, and the length of a branch, hook or repository name.
_MAX_PR_LINKS = 5
_MAX_HOOK_ERRORS = 5
_NAME_MAX_CHARS = 120
_PR_URL_MAX_CHARS = 400

# What opened the newest turn, from the prompt entry's own ``origin.kind`` (or
# the older ``turnOrigin`` spelling): typed by you, a background task that
# finished, or a message from another session.  Mapped through this table so
# an unknown value reads as nothing instead of reaching the UI verbatim.
_TURN_ORIGINS = {
    'human': 'human',
    'task-notification': 'task-notification',
    'task_notification': 'task-notification',
    'peer': 'peer',
}

# Entry types that carry a conversational turn, and therefore the only ones
# whose timestamp counts as session activity - the same types ``_parse`` reads
# ``last_entry_kind`` from, so age and status are driven by the same entries.
# Everything else Claude Code appends (``queue-operation``, ``file-history-*``,
# ``ai-title``, ...) is bookkeeping about the session rather than movement in
# it.  ``system`` is included for the local-command execution record, the one
# subtype that yields a kind; another subtype yields none but is still the
# session doing something, so its timestamp counts too.  Unknown types are
# treated as bookkeeping, the quieter reading: a new conversational type would
# leave the age standing rather than refresh it on something that never
# happened.
_TURN_ENTRY_TYPES = frozenset({'assistant', 'user', 'system'})

_USAGE_MARKER = b'"usage"'
_AI_TITLE_MARKER = b'ai-title'
_CUSTOM_TITLE_MARKER = b'custom-title'
# Matches the field name, which both shapes of the permission mode carry (see
# _absorb_line): the dedicated ``permission-mode`` entry writes it alongside its
# type, and an ordinary entry carries it on its own.  Matching the type name
# instead would see only the first shape.  The wider match admits prompt entries
# (only those carry the field - never a tool result), so the initial full scan
# parses a few hundred more lines, some of them large when a prompt embeds an
# image; measured on a 53 MB transcript that is ~231 ms -> ~240 ms, once per
# session.  The per-second poll is untouched: it parses only appended bytes.
_PERMISSION_MODE_MARKER = b'permissionMode'
_CWD_MARKER = b'"cwd"'
_USER_MARKER = b'"user"'

# Claude Code records a switch into a git worktree (and back out, as a null
# ``worktreeSession``) as an entry of this type.  The session keeps writing its
# transcript under the project it was launched in, while the registry's ``cwd``
# follows it into the worktree - this entry is what ties the two together.
_WORKTREE_STATE_MARKER = b'worktree-state'
_WORKTREE_STATE_TYPE = 'worktree-state'

# A context compaction is recorded as a ``system`` entry of this subtype.  Rare
# lines, so the extra marker costs one more byte search per line - measured at
# about 2% of the first full scan.
_COMPACT_BOUNDARY_MARKER = b'compact_boundary'
_COMPACT_BOUNDARY_SUBTYPE = 'compact_boundary'

# The two markers behind the auto-mode denial streak.  Claude Code records why a
# tool call was refused in its own field, so the streak is read from a control
# value rather than from the refusal's wording.  The tool-result marker is used
# on the raw bytes alone, never parsed: it only has to recognize that *some*
# tool ran, and a transcript's bulk is tool results - parsing each one to learn
# nothing else would cost far more than the byte scan.
_DENIAL_KIND_MARKER = b'"toolDenialKind"'
_TOOL_RESULT_MARKER = b'"tool_result"'

# The one denial kind that counts.  Claude Code writes several (`user-rejected`,
# `permission-rule`, `cancelled`, `automode-unavailable`), and only a classifier
# block counts towards the pause: a denial raised when a safety check refuses
# the classifier's own request is documented as not counting, and the others are
# not the classifier's verdict at all.
_AUTO_MODE_DENIAL_KIND = 'automode-blocked'

# Display length cap for the first-prompt fallback title.
_TITLE_MAX_CHARS = 80

# Display length cap for the one line of an API-error turn's own message that
# the status tooltip shows. Wide enough for the longest wording Claude Code
# writes (the 529 overload sentence, about 150 characters); anything longer - a
# raw JSON body from an edge proxy, say - is clipped rather than shown in full.
_ERROR_DETAIL_MAX_CHARS = 160

# The same cap for the error line of a request the CLI is still retrying - its
# own short summary of the failure ("Connection dropped (ECONNRESET)").
_RETRY_DETAIL_MAX_CHARS = _ERROR_DETAIL_MAX_CHARS

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
    cli_events: list[tuple[str, str, str]] = field(default_factory=list)
    ai_title: str | None = None
    custom_title: str | None = None
    first_prompt: str | None = None
    first_command_prompt: str | None = None
    permission_mode: str | None = None
    # Consecutive and total classifier blocks (see _absorb_line).  Two plain
    # counts: whether they add up to a paused auto mode is a classification and
    # therefore the UI's to make.
    auto_denials_consecutive: int = 0
    auto_denials_total: int = 0
    # The newest worktree-state entry; None before any, and again after the
    # session left its worktree.
    worktree: WorktreeState | None = None
    # Output tokens already summed per API message id (see _unclaimed_usage).
    counted_output_by_message: dict[str, int] = field(default_factory=dict)
    # Distinct files a file-writing tool was called on (see _FILE_EDIT_TOOLS),
    # kept only to be counted, and the number of context compactions.
    edited_files: set[str] = field(default_factory=set)
    compactions: int = 0
    # Usage per skill a turn ran under (see _SKILL_NAME_PATTERN) and model, and
    # the web searches made - both only for the cost tooltip.
    by_skill: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    web_searches: int = 0
    # Memoized run-compressed timelines, one per value field, each kept with the
    # event count it was built from.  Deliberately the last field, so the
    # positional ``copy()`` below stays correct without carrying it over - a copy
    # exists to absorb one more line, so its timelines have to be rebuilt anyway.
    timeline_cache: dict[tuple[str, ...], tuple[int, list[dict[str, str]]]] = field(default_factory=dict)

    def title(self) -> str | None:
        # The housekeeping-command name is the last resort: anything later that
        # can actually name the session (a prompt, a meaningful command) wins.
        return self.custom_title or self.ai_title or self.first_prompt or self.first_command_prompt

    def timeline(self, keys: tuple[str, ...], events: Sequence[tuple[str, ...]]) -> list[dict[str, str]]:
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
        keys : tuple of str
            Names the values are reported under in each entry, and the cache key.
        events : sequence of tuple of str
            The ``(timestamp, *values)`` events accumulated so far.
        """
        cached = self.timeline_cache.get(keys)
        if cached is None or cached[0] != len(events):
            cached = (len(events), _run_timeline(events, keys))
            self.timeline_cache[keys] = cached

        return [dict(entry) for entry in cached[1]]

    def copy(self) -> '_ScanState':
        return _ScanState(
            self.consumed,
            dict(self.totals),
            {model: dict(usage) for model, usage in self.by_model.items()},
            list(self.model_events),
            list(self.cli_events),
            self.ai_title, self.custom_title, self.first_prompt, self.first_command_prompt, self.permission_mode,
            self.auto_denials_consecutive, self.auto_denials_total, self.worktree, dict(self.counted_output_by_message),
            set(self.edited_files), self.compactions, _copy_skill_usage(self.by_skill), self.web_searches,
        )

    def usage(self) -> UsageScan:
        """Return the usage this state holds as a :class:`UsageScan` that shares no dict with it."""
        return UsageScan(
            by_model={model: dict(usage) for model, usage in self.by_model.items()},
            by_skill=_copy_skill_usage(self.by_skill),
            web_searches=self.web_searches,
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
    auto_denials_consecutive: int
    auto_denials_total: int
    worktree: WorktreeState | None
    files_edited: int
    compactions: int
    usage_by_skill: dict[str, dict[str, dict[str, int]]]
    web_searches: int


@dataclass(frozen=True)
class WorktreeState:
    """The git worktree a session switched into, as its newest ``worktree-state`` entry records it.

    Attributes
    ----------
    launch_cwds : tuple of str
        The directories the session was launched from, as Claude Code records
        them (``originalCwd``, then ``preEnterOriginalCwd``) - the project the
        transcript stays filed under.  The two usually differ only in the case
        of a drive letter, which matters when matching a project-folder slug.
    path : str
        The worktree directory the session works in.
    branch : str or None
        The worktree's branch, when the entry names one.
    """

    launch_cwds: tuple[str, ...]
    path: str
    branch: str | None = None


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
        happens to repeat across them.  The scans of the session's subagent
        transcripts (see :func:`usage_scan`) are kept with it: they all lie in
        the folder named after the transcript.
    """
    keep = _ScanKeys()
    for root, session_id, cwd in active:
        if session_id and cwd:
            keep.add(os.path.normcase(str(transcript_path(root, session_id, cwd))))

    with _scan_lock:
        for key in list(_scan_cache):
            if key not in keep:
                del _scan_cache[key]

    # A queued scan of a session that has left the registry would only produce
    # an entry this function drops on its next call.
    drop_pending(keep)


class _ScanKeys:
    """The scan-cache keys of a set of session transcripts and of every file in their session folders."""

    def __init__(self) -> None:
        self._transcripts: set[str] = set()
        self._folders: tuple[str, ...] = ()

    def add(self, transcript_key: str) -> None:
        self._transcripts.add(transcript_key)
        # The session folder is the transcript's path without its extension.
        self._folders += (os.path.splitext(transcript_key)[0] + os.sep,)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and (key in self._transcripts or key.startswith(self._folders))


@dataclass(frozen=True)
class ApiRetry:
    """A failed API request the CLI is retrying, from its trailing ``system``/``api_error`` entry."""

    attempt: int
    max_attempts: int
    # Claude Code's own verdict that the machine is offline, as opposed to one
    # dropped connection or a server-side failure.
    offline: bool = False
    # The CLI's one-line summary of the failure, clipped - CLI text, not
    # conversation, shown in the status tooltip alone.
    detail: str | None = None


@dataclass(frozen=True)
class HookError:
    """A hook that failed without blocking, as Claude Code records it: the hook's name, its exit code, and how often."""

    hook: str
    exit_code: int | None
    count: int


@dataclass(frozen=True)
class PullRequest:
    """A pull request Claude Code linked to the session: its repository, number, and web address as recorded."""

    repository: str
    number: int
    url: str | None = None


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
    # The trailing API-error turn's own error token and HTTP status, exactly as
    # Claude Code writes them; both absent when the newest entry is not an error.
    # Naming the cause from them (a usage limit, an overload, a rejected request)
    # is a classification and therefore the UI's job.
    api_error_kind: str | None = None
    api_error_status: int | None = None
    # The first line of that error turn's own message, truncated - the one thing
    # the two fields above cannot express (when a limit resets, which of the
    # invalid-request cases applied). Claude Code writes this line itself, so it
    # is CLI text, not conversation, and it is shown verbatim in the status
    # tooltip alone - never parsed, and never for any other entry type.
    api_error_detail: str | None = None
    # A usage limit's own reset: which limit was hit (``five_hour``,
    # ``seven_day``, ...) and when it lifts, in POSIX seconds - both read from the
    # trailing error entry's ``quotaLimits``, both absent for any other error.
    rate_limit_type: str | None = None
    rate_limit_resets_at: float | None = None
    # Set while the newest entry is a failed request the CLI is retrying.
    api_retry: ApiRetry | None = None
    # When a scheduled ``/loop`` run is due, in POSIX seconds: the newest
    # wakeup call's time plus its delay.  Kept even once past: the snapshot
    # turns it into seconds from now, and a past one simply no longer applies.
    wakeup_at: float | None = None
    # How the newest turn ended in Claude Desktop's wrap-up call: ``blocked``
    # (cannot go on without your decision), ``ask`` (ends on a question for
    # you), or None.  Cleared by any later tool call or prompt.
    turn_end_signal: str | None = None
    # What opened the newest turn (see _TURN_ORIGINS), or None when unknown.
    turn_origin: str | None = None
    # Display details from the tail, each the newest value Claude Code wrote (see
    # _TailDetails): the git branch, the reply's effort level and speed, how many
    # tokens the newest reply's context held and on which model, the session's
    # dollar budget, prompts still queued, Claude Desktop's progress list (the
    # 1-based step in progress and the step count), the hooks that failed, the
    # artifact count, and the pull requests Claude Code linked.
    git_branch: str | None = None
    effort: str | None = None
    speed: str | None = None
    context_tokens: int | None = None
    context_model: str | None = None
    budget_used: float | None = None
    budget_total: float | None = None
    queued: int = 0
    plan_step: int | None = None
    plan_total: int = 0
    hook_errors: tuple[HookError, ...] = ()
    artifact_count: int | None = None
    pr_links: tuple[PullRequest, ...] = ()
    # From the full scan: distinct files edited and context compactions, both
    # None while that scan is still pending.
    files_edited: int | None = None
    compactions: int | None = None
    age_seconds: float | None = None
    title: str | None = None
    model: str | None = None
    cli_version: str | None = None
    usage: dict[str, int] | None = None
    usage_by_model: dict[str, dict[str, int]] | None = None
    # Usage per skill and model, and the web searches made; None/0 while the
    # full scan is pending, like the usage above.
    usage_by_skill: dict[str, dict[str, dict[str, int]]] | None = None
    web_searches: int = 0
    model_timeline: list[dict[str, str]] | None = None
    cli_timeline: list[dict[str, str]] | None = None
    permission_mode: str | None = None
    # How many tool calls the auto-mode classifier blocked in a row, and in the
    # session overall.  Counted from the denial entries' own ``toolDenialKind``
    # field; no part of the refusal's text is read.
    auto_denials_consecutive: int = 0
    auto_denials_total: int = 0
    # True while the transcript's first full scan still runs in the background:
    # usage and both timelines are then None - unknown rather than empty - the
    # denial counts read zero (the calmer reading: no pause badge), and the
    # title and mode are the newest ones near the end of the file.
    scan_pending: bool = False


@dataclass(frozen=True)
class HistoryState:
    """Display metadata for a past (non-live) session transcript."""

    session_id: str
    cwd: str | None = None
    title: str | None = None
    model: str | None = None
    cli_version: str | None = None
    git_branch: str | None = None
    age_seconds: float | None = None
    usage: UsageScan = field(default_factory=UsageScan)


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

    scan = _scan_or_defer(path)
    age_seconds = _activity_age(state.last_timestamp, mtime)
    if scan is None:
        title, permission_mode = _recent_title_and_mode(path)
        return replace(state, age_seconds=age_seconds, title=title, permission_mode=permission_mode, scan_pending=True)

    return replace(state, age_seconds=age_seconds, usage=scan.usage, usage_by_model=scan.usage_by_model,
                   usage_by_skill=scan.usage_by_skill, web_searches=scan.web_searches,
                   model_timeline=scan.model_timeline, cli_timeline=scan.cli_timeline,
                   title=scan.title, permission_mode=scan.permission_mode,
                   auto_denials_consecutive=scan.auto_denials_consecutive,
                   auto_denials_total=scan.auto_denials_total,
                   files_edited=scan.files_edited, compactions=scan.compactions)


def worktree_for(path: Path) -> WorktreeState | None:
    """Return the worktree the session behind transcript *path* currently works in, or None.

    Runs the same incremental scan :func:`state_for` uses, so the file is read
    in full once and the scan cache it leaves behind serves the next
    :func:`state_for` call for the same file.
    """
    return _scan_appended(path).worktree


def history_state_for(path: Path) -> HistoryState:
    """Return display metadata for a past session transcript, keyed by its file.

    Unlike :func:`state_for` - which begins from a live registry record and
    needs only the tail plus an incremental usage scan - a history entry is
    discovered by walking ``projects/`` and has no registry record, so its
    ``cwd`` (used to group the session under its project) is unknown up front
    and must be recovered from the transcript itself.

    The correct title can sit anywhere in the file (a late rename writes its
    entry at that point, not at the head), and the cost needs every reply's
    usage, so the whole file runs once through the same scan a live session
    gets (:func:`_scan_history`), plus the first ``cwd``.  The current model,
    the branch and the activity age come from a cheap tail read.

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

    scan, cwd = _scan_history(path)
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

    return HistoryState(
        session_id=session_id, cwd=cwd, title=scan.title(), model=tail.model, cli_version=tail.cli_version,
        git_branch=tail.git_branch, age_seconds=age_seconds, usage=scan.usage(),
    )


def usage_scan(path: Path) -> UsageScan | None:
    """Return the usage in a live session's subagent transcript, or None while its first scan runs in the background.

    The incremental, cached scan :func:`state_for` uses, on a state that never
    looks for a first prompt: a subagent's prompt is not the session's title,
    and only the usage of this file is wanted.
    """
    scan = _scan_or_defer(path, _usage_only_state)
    if scan is None:
        return None

    return UsageScan(by_model=scan.usage_by_model, by_skill=scan.usage_by_skill, web_searches=scan.web_searches)


def usage_scan_once(path: Path) -> UsageScan:
    """Return the usage in a past session's subagent transcript, read once and not cached.

    A file that cannot be read counts as empty - the session's cost is then
    the part that could be read, never an error.
    """
    state = _usage_only_state()
    try:
        _absorb_file(path, state, path.stat().st_size)
    except OSError:
        return UsageScan()

    return state.usage()


def _usage_only_state() -> _ScanState:
    """Return a fresh scan state that skips the search for a first prompt (see :func:`usage_scan`)."""
    return _ScanState(first_prompt='')


def _scan_history(path: Path) -> tuple[_ScanState, str | None]:
    """Read a past transcript once through the full scan, also resolving the session cwd.

    Every line goes through ``_absorb_line``, exactly as a live session's
    first scan, which yields the title (Claude Code's precedence: a manual
    rename, then the generated title, then the first prompt) and the usage the
    cost is priced from.  The first entry carrying a ``cwd`` is parsed once
    more for that path.  Read in chunks, so a large transcript is never held
    in memory whole.  An unreadable file yields an empty state and no cwd.
    """
    state = _ScanState()
    cwd: str | None = None

    try:
        with path.open('rb') as handle:
            remainder = b''
            while chunk := handle.read(_SCAN_CHUNK_BYTES):
                lines = (remainder + chunk).split(b'\n')
                remainder = lines.pop()
                for raw_line in lines:
                    if cwd is None and _CWD_MARKER in raw_line:
                        cwd = _entry_cwd(raw_line)
                    _absorb_line(raw_line, state)
    except OSError:
        return _ScanState(), None

    if remainder:
        if cwd is None and _CWD_MARKER in remainder:
            cwd = _entry_cwd(remainder)
        _absorb_line(remainder, state)

    return state, cwd


def _entry_cwd(raw_line: bytes) -> str | None:
    """Return the ``cwd`` of the entry on *raw_line*, or None."""
    entry = _load(raw_line.decode('utf-8', errors='ignore'))
    value = entry.get('cwd') if entry is not None else None
    return value if isinstance(value, str) and value else None


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
    api_error_kind: str | None = None
    api_error_status: int | None = None
    api_error_detail: str | None = None
    model: str | None = None
    cli_version: str | None = None
    rate_limit_type: str | None = None
    rate_limit_resets_at: float | None = None
    api_retry: ApiRetry | None = None
    wakeup_at: float | None = None
    turn_end_signal: str | None = None
    turn_origin: str | None = None
    any_parsed: bool = False
    details = _TailDetails()

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

        entry_type = entry.get('type')
        message = entry.get('message')
        content = message.get('content') if isinstance(message, dict) else None

        # A prompt opens a new turn, and whatever the previous turn ended on is
        # over with it.  Read ahead of the isMeta skip below: Claude Code injects
        # a peer's message and a loop's wakeup as meta entries, and both open a
        # turn all the same.
        if entry_type == 'user' and not _carries_tool_result(content):
            turn_origin = _turn_origin(entry)
            turn_end_signal = None

        # Notices Claude Code injects into the conversation (the local-command
        # "DO NOT respond" caveat, continuation summaries, ...) carry isMeta.
        # They are not conversational turns, so - like sidechain entries - they
        # must never be read as a prompt the model owes a response to.
        if entry.get('isMeta') is True:
            continue

        # Claude Code also appends bookkeeping entries - queue operations,
        # file-history snapshots and deltas, the generated title - which say
        # nothing about the conversation having moved.  Letting one of those set
        # the age is the very mistake ``_activity_age`` avoids by preferring the
        # entry timestamp over the file mtime, merely arriving as an appended
        # line instead of an in-place rewrite.
        timestamp = entry.get('timestamp')
        if isinstance(timestamp, str) and entry_type in _TURN_ENTRY_TYPES:
            last_timestamp = timestamp

        # The Claude Code version is stamped on every entry, not just assistant
        # turns, so the newest one of any kind is the version currently in use -
        # the counterpart of the model below, which only an assistant turn can
        # report.
        entry_version = entry.get('version')
        if isinstance(entry_version, str) and entry_version:
            cli_version = entry_version

        details.absorb_entry(entry_type, entry)

        if entry_type == 'assistant' and isinstance(message, dict):
            if entry.get('isApiErrorMessage') is True:
                # A locally-generated error turn (a usage/session limit, an
                # overload, or a server error). The turn stopped and nothing is
                # running, so it is its own kind - never the pending assistant
                # turn that a non-end_turn stop_reason would otherwise imply and
                # read as "working". The structural error fields are read, plus
                # the bounded first line of the CLI's own error message (see
                # _api_error_detail) - which is the only entry type whose text is
                # read here at all.
                last_entry_kind = 'api_error'
                last_stop_reason = message.get('stop_reason')
                api_error_kind = _api_error_kind(entry)
                api_error_status = _api_error_status(entry)
                api_error_detail = _api_error_detail(content)
                rate_limit_type, rate_limit_resets_at = _rate_limit_reset(entry)
                # The retries ran out: the request is no longer being retried.
                api_retry = None
            else:
                last_entry_kind = 'assistant'
                # A real turn superseded any earlier API error, so the error
                # fields (set only in the api_error branch) must not linger -
                # they describe the trailing entry alone.  The same holds for a
                # retry: the request went through.
                api_error_kind = None
                api_error_status = None
                api_error_detail = None
                rate_limit_type = rate_limit_resets_at = None
                api_retry = None
                last_stop_reason = message.get('stop_reason')
                entry_model = message.get('model')
                # Keep the last *real* model for the column; the synthetic sentinel
                # (locally-generated turns) is not a model and must not be displayed.
                if isinstance(entry_model, str) and entry_model and entry_model != _SYNTHETIC_MODEL:
                    model = entry_model
                details.absorb_reply(entry, message)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'tool_use':
                            last_tool_id = block.get('id')
                            last_tool_name = block.get('name')
                            # Only the wrap-up call says how the turn ended, so any
                            # other call after it means the turn went on.
                            turn_end_signal = _wrap_up_signal(block)
                            if last_tool_name == _WAKEUP_TOOL:
                                wakeup_at = _wakeup_at(block, timestamp)
                            elif last_tool_name == _PLAN_TOOL:
                                details.absorb_plan_call(block.get('input'))

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
            api_error_kind = None
            api_error_status = None
            api_error_detail = None
            rate_limit_type = rate_limit_resets_at = None
            api_retry = None
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
            api_error_kind = None
            api_error_status = None
            api_error_detail = None
            rate_limit_type = rate_limit_resets_at = None
            api_retry = None

        elif entry_type == 'system' and entry.get('subtype') == 'api_error':
            # A request failed and the CLI is retrying it.  The turn is still
            # running - nothing about the entry kind changes - but the session is
            # waiting on the network rather than thinking, which the label can say.
            api_retry = _api_retry(entry)

    pending_tool = last_tool_id is not None and last_tool_id not in resolved_tool_ids

    return TranscriptState(
        has_transcript=True,
        last_stop_reason=last_stop_reason,
        pending_tool=pending_tool,
        last_tool_name=last_tool_name,
        last_timestamp=last_timestamp,
        last_entry_kind=last_entry_kind,
        any_parsed=any_parsed,
        api_error_kind=api_error_kind,
        api_error_status=api_error_status,
        api_error_detail=api_error_detail,
        rate_limit_type=rate_limit_type,
        rate_limit_resets_at=rate_limit_resets_at,
        api_retry=api_retry,
        wakeup_at=wakeup_at,
        turn_end_signal=turn_end_signal,
        turn_origin=turn_origin,
        model=model,
        cli_version=cli_version,
        **details.state_fields(),
    )


@dataclass
class _TailDetails:
    """The display details :func:`_parse` collects beside the status, over the main conversation's entries in the tail.

    Each is the newest value Claude Code wrote, or a count over the window -
    nothing here feeds the status.  Values are control fields and numbers;
    of the hooks, the plan and the queue only names, statuses and operations
    are read, never a hook's output, a step's text or a queued prompt.
    """

    git_branch: str | None = None
    effort: str | None = None
    speed: str | None = None
    context_tokens: int | None = None
    context_model: str | None = None
    budget_used: float | None = None
    budget_total: float | None = None
    queued: int = 0
    plan_statuses: list[str] = field(default_factory=list)
    plan_current: int | None = None
    hook_error_counts: dict[tuple[str, int | None], int] = field(default_factory=dict)
    artifact_count: int | None = None
    pull_requests: dict[tuple[str, int], str | None] = field(default_factory=dict)

    def absorb_entry(self, entry_type: object, entry: dict) -> None:
        """Fold the branch stamped on any entry, and the bookkeeping entries that carry a detail."""
        branch = entry.get('gitBranch')
        if isinstance(branch, str) and branch:
            self.git_branch = _clip(branch, _NAME_MAX_CHARS)

        if entry_type == 'attachment':
            self._absorb_attachment(entry.get('attachment'))
        elif entry_type == 'queue-operation':
            # A dequeue or removal of a prompt queued before the window is
            # ignored rather than counted below zero.
            operation = entry.get('operation')
            if operation == 'enqueue':
                self.queued += 1
            elif operation in ('dequeue', 'remove'):
                self.queued = max(0, self.queued - 1)
        elif entry_type == 'pr-link':
            self._absorb_pull_request(entry)
        elif entry_type == 'frame-link':
            count = entry.get('artifactCount')
            if _is_count(count):
                self.artifact_count = count

    def absorb_reply(self, entry: dict, message: dict) -> None:
        """Fold a real (not error, not synthetic) assistant reply: its effort, speed, and context size."""
        effort = entry.get('effort')
        if isinstance(effort, str) and _LABEL_TOKEN_PATTERN.match(effort):
            self.effort = effort

        usage = message.get('usage')
        model = message.get('model')
        if not isinstance(usage, dict) or not isinstance(model, str) or not model or model == _SYNTHETIC_MODEL:
            return

        speed = usage.get('speed')
        if isinstance(speed, str) and _LABEL_TOKEN_PATTERN.match(speed):
            self.speed = speed

        # What the request sent is what the context held: fresh input plus the
        # cached prefix, read or written.
        context = 0
        for key in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
            value = usage.get(key)
            if _is_count(value):
                context += value
        if context > 0:
            self.context_tokens = context
            self.context_model = model

    def absorb_plan_call(self, tool_input: object) -> None:
        """Apply one call of Claude Desktop's progress tool to the plan (see _PLAN_TOOL)."""
        if not isinstance(tool_input, dict):
            return

        items = tool_input.get('items')
        if isinstance(items, list):
            self.plan_statuses = [_plan_status(item) for item in items if isinstance(item, dict)]
            self.plan_current = _index_of(self.plan_statuses, _PLAN_IN_PROGRESS)
            return

        statuses = self.plan_statuses
        if tool_input.get('advance') is True:
            current = _index_of(statuses, _PLAN_IN_PROGRESS)
            if current is not None:
                statuses[current] = _PLAN_COMPLETED
            following = _index_of(statuses, _PLAN_PENDING, 0 if current is None else current + 1)
            if following is not None:
                statuses[following] = _PLAN_IN_PROGRESS
            self.plan_current = following
            return

        done = tool_input.get('done')
        for number in done if isinstance(done, list) else ():
            if _is_count(number) and 1 <= number <= len(statuses):
                statuses[number - 1] = _PLAN_COMPLETED
        start = tool_input.get('start')
        if _is_count(start) and 1 <= start <= len(statuses):
            statuses[start - 1] = _PLAN_IN_PROGRESS
            self.plan_current = start - 1
        elif self.plan_current is not None and statuses[self.plan_current] == _PLAN_COMPLETED:
            self.plan_current = None

    def state_fields(self) -> dict[str, object]:
        """Return these details under their :class:`TranscriptState` field names."""
        unfinished = self.plan_current is not None and any(status != _PLAN_COMPLETED for status in self.plan_statuses)
        hook_errors = sorted(self.hook_error_counts.items(), key=lambda item: -item[1])[:_MAX_HOOK_ERRORS]
        pull_requests = list(self.pull_requests.items())[-_MAX_PR_LINKS:]

        return {
            'git_branch': self.git_branch,
            'effort': self.effort,
            'speed': self.speed,
            'context_tokens': self.context_tokens,
            'context_model': self.context_model,
            'budget_used': self.budget_used,
            'budget_total': self.budget_total,
            'queued': self.queued,
            'plan_step': self.plan_current + 1 if unfinished else None,
            'plan_total': len(self.plan_statuses) if unfinished else 0,
            'hook_errors': tuple(HookError(hook, code, count) for (hook, code), count in hook_errors),
            'artifact_count': self.artifact_count,
            'pr_links': tuple(PullRequest(repository, number, url) for (repository, number), url in reversed(pull_requests)),
        }

    def _absorb_attachment(self, attachment: object) -> None:
        """Fold a budget notice or a failed-hook notice; every other attachment, and their text, stay unread."""
        if not isinstance(attachment, dict):
            return

        kind = attachment.get('type')
        if kind == 'budget_usd':
            used = attachment.get('used')
            total = attachment.get('total')
            if _is_amount(used) and _is_amount(total) and total > 0:
                self.budget_used = float(used)
                self.budget_total = float(total)
        elif kind == 'hook_non_blocking_error':
            # The hook's own output and command stay unread: only which hook
            # failed and with which exit code.
            hook = attachment.get('hookName')
            code = attachment.get('exitCode')
            if isinstance(hook, str) and hook:
                key = (_clip(hook, _NAME_MAX_CHARS), code if _is_count(code) else None)
                self.hook_error_counts[key] = self.hook_error_counts.get(key, 0) + 1

    def _absorb_pull_request(self, entry: dict) -> None:
        """Fold a ``pr-link`` entry; one pull request linked again moves to the newest position."""
        repository = entry.get('prRepository')
        number = entry.get('prNumber')
        if not isinstance(repository, str) or not repository or not _is_count(number) or number <= 0:
            return

        # Passed on as recorded and judged by the UI, which alone decides whether
        # an address is safe to link (logic.pullRequestUrl).
        url = entry.get('prUrl')
        if not isinstance(url, str) or not url or len(url) > _PR_URL_MAX_CHARS:
            url = None
        key = (_clip(repository, _NAME_MAX_CHARS), number)
        self.pull_requests.pop(key, None)
        self.pull_requests[key] = url


def _plan_status(item: dict) -> str:
    """Return a plan item's status, reading anything unexpected as still pending."""
    status = item.get('status')
    return status if status in (_PLAN_COMPLETED, _PLAN_IN_PROGRESS) else _PLAN_PENDING


def _index_of(statuses: list[str], wanted: str, start: int = 0) -> int | None:
    """Return the index of the first *wanted* status at or after *start*, or None."""
    for index in range(start, len(statuses)):
        if statuses[index] == wanted:
            return index

    return None


def _is_count(value: object) -> bool:
    """Return True for a non-negative integer that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_amount(value: object) -> bool:
    """Return True for a finite, non-negative number that is not a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return value >= 0 and value != float('inf')


def _api_error_kind(entry: dict) -> str | None:
    """Return an API-error entry's error token, or None if it carries none.

    The token is Claude Code's own short name for the failure (``rate_limit``,
    ``server_error``, ``invalid_request``, ``authentication_failed``, ...).  It
    is passed through verbatim - what it means for the label is decided in the
    UI, like every other classification.
    """
    kind = entry.get('error')
    if isinstance(kind, str) and kind:
        return kind

    return None


def _api_error_status(entry: dict) -> int | None:
    """Return an API-error entry's HTTP status, or None if it carries none.

    A failure that never reached the API (a lost connection) has no status at
    all, and the field has been seen both as a number and as a string, so both
    readings are accepted and anything else degrades to None.
    """
    status = entry.get('apiErrorStatus')
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str) and status.strip().isdigit():
        return int(status.strip())

    return None


def _api_error_detail(content: object) -> str | None:
    """Return the first line of an API-error turn's own message, truncated.

    This is a sanctioned, bounded read of message text, and the only one the
    status needs: the wording carries what the structural fields cannot - which
    limit was hit and when it resets, or which of the invalid-request cases
    applied - and Claude Code generates the line itself, so it is CLI text
    rather than conversation.  Only the first text block's first non-empty line
    is read, clipped to ``_ERROR_DETAIL_MAX_CHARS``, and it is passed through
    verbatim: the label above it comes from the structural fields, so this line
    is never parsed for meaning.  Called for an ``isApiErrorMessage`` entry
    alone; no other entry type's text is read here.
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
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return _clip(stripped, _ERROR_DETAIL_MAX_CHARS)

    return None


def _clip(text: str, limit: int) -> str:
    """Return *text* cut to *limit* characters, ending in an ellipsis when it had to be cut."""
    if len(text) <= limit:
        return text

    return text[:limit - 1] + '…'


def _carries_tool_result(content: object) -> bool:
    """Return True if a user entry's content answers a tool call rather than opening a turn."""
    if not isinstance(content, list):
        return False

    return any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content)


def _turn_origin(entry: dict) -> str | None:
    """Return what opened the turn a prompt entry starts, or None when the entry does not say.

    Only the entry's own origin label is read - ``origin.kind``, or the older
    ``turnOrigin`` - never the ``origin`` payload around it, which for a message
    from another session carries that message's text.
    """
    origin = entry.get('origin')
    kind = origin.get('kind') if isinstance(origin, dict) else None
    if not isinstance(kind, str):
        kind = entry.get('turnOrigin')

    return _TURN_ORIGINS.get(kind) if isinstance(kind, str) else None


def _wrap_up_signal(block: dict) -> str | None:
    """Return how a Claude Desktop wrap-up call ends the turn (``blocked``/``ask``), or None.

    Any other tool call returns None, so a later call clears an earlier signal.
    Both inputs are read as flags only: whether ``blocked`` is set, and whether
    an ``ask`` was given at all - never what it asks.
    """
    if block.get('name') != _WRAP_UP_TOOL:
        return None

    tool_input = block.get('input')
    if not isinstance(tool_input, dict):
        return None
    if tool_input.get('blocked') is True:
        return 'blocked'
    ask = tool_input.get('ask')
    if isinstance(ask, str) and ask.strip():
        return 'ask'

    return None


def _wakeup_at(block: dict, timestamp: object) -> float | None:
    """Return when a ``ScheduleWakeup`` call is due, in POSIX seconds, or None.

    A call that stops the loop, or one whose delay or timestamp does not read
    as a number, schedules nothing.  Only ``stop`` and ``delaySeconds`` are read.
    """
    tool_input = block.get('input')
    if not isinstance(tool_input, dict) or tool_input.get('stop') is True:
        return None

    delay = tool_input.get('delaySeconds')
    scheduled = _timestamp_epoch(timestamp) if isinstance(timestamp, str) else None
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay <= 0 or scheduled is None:
        return None

    return scheduled + float(delay)


def _api_retry(entry: dict) -> ApiRetry | None:
    """Return the retry a ``system``/``api_error`` entry records, or None if it does not read as one.

    The attempt counters and the offline flag are structural; the one text read
    is the CLI's own short ``formatted`` summary of the failure, first line only
    and clipped - the same kind of line as ``_api_error_detail``.
    """
    attempt = entry.get('retryAttempt')
    max_attempts = entry.get('maxRetries')
    for value in (attempt, max_attempts):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None

    error = entry.get('error')
    error = error if isinstance(error, dict) else {}
    formatted = error.get('formatted')
    detail = None
    if isinstance(formatted, str):
        lines = [line.strip() for line in formatted.splitlines() if line.strip()]
        detail = _clip(lines[0], _RETRY_DETAIL_MAX_CHARS) if lines else None

    return ApiRetry(attempt=attempt, max_attempts=max_attempts, offline=error.get('isNetworkDown') is True, detail=detail)


def _rate_limit_reset(entry: dict) -> tuple[str | None, float | None]:
    """Return which usage limit an API-error entry hit and when it resets, from its ``quotaLimits``.

    Either value degrades to None on its own: a limit with no readable reset
    still names itself, and a reset with no limit type is still a reset.
    """
    limits = entry.get('quotaLimits')
    if not isinstance(limits, dict):
        return None, None

    limit_type = limits.get('rateLimitType')
    resets_at = limits.get('resetsAt')
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)) or resets_at <= 0:
        resets_at = None

    return (limit_type if isinstance(limit_type, str) and limit_type else None), (float(resets_at) if resets_at is not None else None)


def _scan_appended(path: Path, fresh: Callable[[], _ScanState] = _ScanState) -> _ScanResult:
    """Return the incremental scan's accumulated result for *path*.

    The first call reads the whole file; subsequent calls parse only newly
    appended bytes.  Tracks summed usage (overall and per model - subagents
    often run on a cheaper model, so a valid cost needs the split), the ordered
    model-switch and CLI-version timelines of the main conversation, the display
    title, and the latest permission mode.  *fresh* makes the state a scan
    starts from (see :func:`usage_scan`).
    """
    # Normalize case so two registry cwds that differ only in case (they resolve
    # to the same case-insensitive file) share one cache entry instead of each
    # paying a full-file scan.
    cache_key = os.path.normcase(str(path))

    with _scan_lock:
        state = _scan_cache.get(cache_key) or fresh()

        try:
            size = path.stat().st_size
        except OSError:
            return _scan_result(state)

        if size < state.consumed:
            state = fresh()

        result = state

        if size > state.consumed:
            # A failed read leaves a cached state consistent (see _absorb_file);
            # a fresh one is simply not stored, so nothing is counted twice.
            try:
                trailing = _absorb_file(path, state, size)
            except OSError:
                return _scan_result(state)

            # The trailing bytes may be a line still being written (or a file
            # without a trailing newline): reflect them in the result, but keep
            # them out of the cache so they are re-read - never double-counted -
            # on the next poll.
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


def _absorb_file(path: Path, state: _ScanState, end: int, before_chunk: Callable[[], None] | None = None) -> bytes:
    """Fold the complete lines of *path* between ``state.consumed`` and *end* into *state*.

    Read in bounded chunks, so a transcript of hundreds of megabytes is never
    held in memory whole.  ``state.consumed`` advances after every chunk to the
    end of the last complete line folded in, so a read that fails midway leaves
    it describing exactly what *state* holds.  Returns the partial line left
    over at *end*, which is not folded in.  Raises ``OSError`` if the file
    cannot be read.

    *before_chunk*, when given, runs before every chunk is read - the point at
    which a background scan gives way (see
    :func:`~agent_monitor_for_claude.background_scans.pause_background_scans`).
    """
    remainder = b''
    with path.open('rb') as handle:
        position = handle.seek(state.consumed)
        while position < end:
            if before_chunk is not None:
                before_chunk()
            chunk = handle.read(min(_SCAN_CHUNK_BYTES, end - position))
            if not chunk:
                break
            position += len(chunk)

            lines = (remainder + chunk).split(b'\n')
            remainder = lines.pop()
            for raw_line in lines:
                _absorb_line(raw_line, state)
            state.consumed = position - len(remainder)

    return remainder


def _scan_or_defer(path: Path, fresh: Callable[[], _ScanState] = _ScanState) -> _ScanResult | None:
    """Return :func:`_scan_appended`'s result for *path*, or None while its first full scan runs in the background.

    Only the *first* scan of a large transcript is deferred; it is what costs
    seconds, and every later one parses just the appended bytes.  A snapshot
    that met the scan synchronously would keep the whole window on its loading
    skeleton until the largest transcript had been read - the deferred one
    instead lets it show every session at once and fill in the rest when the
    scan has stored its result.  The queue and its thread live in
    :mod:`~agent_monitor_for_claude.background_scans`; :func:`_first_scan` is
    the scan it runs.
    """
    cache_key = os.path.normcase(str(path))
    with _scan_lock:
        scanned = cache_key in _scan_cache

    if not scanned:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > _SYNC_SCAN_BYTES:
            queue_scan(cache_key, path, size, partial(_first_scan, fresh=fresh))
            return None

    return _scan_appended(path, fresh)


def _first_scan(cache_key: str, path: Path, fresh: Callable[[], _ScanState] = _ScanState) -> bool:
    """Scan *path* in full and store the result under *cache_key*; return True if it was stored.

    The read runs outside ``_scan_lock`` - it can take seconds, and holding the
    lock would stall every snapshot behind it - which is safe because the state
    being filled is private until it is stored.  A key some other caller has
    filled in the meantime (``worktree_for`` scans synchronously) is left as it
    is: that state has already moved on from where this one would start.
    """
    with _scan_lock:
        if cache_key in _scan_cache:
            return False

    state = fresh()
    try:
        _absorb_file(path, state, path.stat().st_size, wait_for_readers)
    except OSError:
        return False

    with _scan_lock:
        if cache_key in _scan_cache:
            return False
        _scan_cache[cache_key] = state

    return True


def _recent_title_and_mode(path: Path) -> tuple[str | None, str | None]:
    """Return the newest title and permission mode within the last ``_PENDING_LOOKBACK_BYTES`` of *path*.

    The stand-in for a first full scan still running in the background.  Both
    are last-wins fields of that scan, so the newest occurrence is its exact
    value whenever it lies inside the window, and each candidate line goes
    through ``_absorb_line`` on a throwaway state, so a value is accepted by the
    very rules the full scan applies.  What the window cannot see: a manual
    rename older than it, which leaves the generated title showing until the
    scan completes, and the first prompt - the window's oldest prompt is not the
    session's first - so a session named only by its prompt shows its short
    name until then.
    """
    custom_title: str | None = None
    ai_title: str | None = None
    permission_mode: str | None = None
    try:
        for raw_line in _lines_newest_first(path, _PENDING_LOOKBACK_BYTES):
            if not (_PERMISSION_MODE_MARKER in raw_line or _AI_TITLE_MARKER in raw_line or _CUSTOM_TITLE_MARKER in raw_line):
                continue

            # A known first prompt keeps _absorb_line from parsing every user
            # entry in search of one, which this read has no use for.
            probe = _ScanState(first_prompt='')
            _absorb_line(raw_line, probe)
            custom_title = custom_title or probe.custom_title
            ai_title = ai_title or probe.ai_title
            permission_mode = permission_mode or probe.permission_mode
            # A rename outranks the generated title, so only finding one ends
            # the search for a title.
            if custom_title and permission_mode:
                break
    except OSError:
        pass

    return custom_title or ai_title, permission_mode


def _lines_newest_first(path: Path, max_bytes: int) -> Iterator[bytes]:
    """Yield the lines of the last *max_bytes* of *path*, newest first, dropping a leading partial line."""
    with path.open('rb') as handle:
        position = handle.seek(0, os.SEEK_END)
        floor = max(0, position - max_bytes)
        partial = b''
        while position > floor:
            start = max(floor, position - _LOOKBACK_CHUNK_BYTES)
            handle.seek(start)
            lines = (handle.read(position - start) + partial).split(b'\n')
            position = start
            # The first piece may continue in the chunk before this one.
            partial = lines.pop(0)
            yield from reversed(lines)

        if floor == 0:
            yield partial


def _scan_result(state: _ScanState) -> _ScanResult:
    """Snapshot a scan state into plain, JSON-serializable return values."""
    by_model = {model: dict(usage) for model, usage in state.by_model.items()}
    return _ScanResult(
        usage=dict(state.totals),
        usage_by_model=by_model,
        model_timeline=state.timeline(_MODEL_KEYS, state.model_events),
        cli_timeline=state.timeline(_CLI_KEYS, state.cli_events),
        title=state.title(),
        permission_mode=state.permission_mode,
        auto_denials_consecutive=state.auto_denials_consecutive,
        auto_denials_total=state.auto_denials_total,
        worktree=state.worktree,
        files_edited=len(state.edited_files),
        compactions=state.compactions,
        usage_by_skill=_copy_skill_usage(state.by_skill),
        web_searches=state.web_searches,
    )


def _copy_skill_usage(by_skill: dict[str, dict[str, dict[str, int]]]) -> dict[str, dict[str, dict[str, int]]]:
    """Return a copy of per-skill, per-model usage that shares no dict with *by_skill*."""
    return {skill: {model: dict(usage) for model, usage in models.items()} for skill, models in by_skill.items()}


def _run_timeline(events: Sequence[tuple[str, ...]], keys: tuple[str, ...]) -> list[dict[str, str]]:
    """Sort ``(timestamp, *values)`` events by time and collapse runs of equal values.

    Transcript entries are not strictly ordered on disk, so the events are sorted
    by time first, then runs of the same values are collapsed to a single entry
    carrying the moment that run began.  The result is a chronological switch
    log: one entry per *run*, so a value used, left, and returned to appears more
    than once - and the final entry is the values in use, with the time they were
    last switched to.  A run ends when *any* of the values changes.

    Parameters
    ----------
    events : sequence of tuple of str
        Raw events in on-disk order, which is not necessarily chronological.
    keys : tuple of str
        Names the values are reported under in each resulting entry, in the
        order the events hold them.  An empty value is left out of its entry.
    """
    # Sort by parsed epoch, not the raw string: lexicographic order matches
    # chronological order only while every timestamp has an identical shape, but
    # a fractional-seconds value ('...07.500Z') sorts before a whole-second one
    # ('...07Z') though it is later, and an explicit offset mis-sorts against 'Z'.
    # The raw string is kept for display; it breaks ties for equal epochs.
    timeline: list[dict[str, str]] = []
    last_values: tuple[str, ...] | None = None
    for event in sorted(events, key=lambda event: (_timestamp_epoch(event[0]) or 0.0, event[0])):
        values = event[1:]
        if values == last_values:
            continue
        entry = {'time': event[0]}
        entry.update((key, value) for key, value in zip(keys, values) if value)
        timeline.append(entry)
        last_values = values

    return timeline


def _add_usage(totals: dict[str, int], key: str, value: object) -> None:
    """Add a positive integer usage *value* to ``totals[key]``, ignoring anything else."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        totals[key] += value


def _unclaimed_usage(message: dict[str, object], usage: dict[str, object], state: _ScanState) -> dict[str, object]:
    """Return the part of *usage* not yet summed for *message*'s API response, and record it as summed.

    Claude Code writes one API response as several transcript entries - one per
    content block (thinking, text, each tool call) - and every one of them
    repeats the response's ``usage``.  Summing each entry counted a session's
    tokens and cost about twice over.  The response's ``message.id`` ties the
    entries together: its first entry counts in full, a later one only by the
    output tokens it adds - an early entry can carry a mid-stream output count,
    while the input and cache figures are the same in every one.  An entry
    without a usable id cannot be matched to its siblings and counts on its own.
    """
    message_id = message.get('id')
    if not isinstance(message_id, str) or not message_id:
        return usage

    output = usage.get('output_tokens')
    if not isinstance(output, int) or isinstance(output, bool) or output < 0:
        output = 0

    counted = state.counted_output_by_message.get(message_id)
    if counted is None:
        state.counted_output_by_message[message_id] = output
        return usage

    if output <= counted:
        return {}

    state.counted_output_by_message[message_id] = output
    return {'output_tokens': output - counted}


def _absorb_line(raw_line: bytes, state: _ScanState) -> None:
    """Fold one transcript line into the scan state (usage, title, mode, denials)."""
    # A tool result that is not a denial means a tool ran, which ends the streak
    # (Claude Code resets the consecutive count on any allowed action).  Decided
    # on the raw bytes and settled before the pre-filter below, because the reset
    # has to see every tool result while the pre-filter is there precisely to
    # leave them unparsed.  The looseness is one-directional and deliberate: a
    # sidechain's tool result resets the main streak too, and a match is not
    # proof the entry parses - both can only clear a streak early, never invent
    # one, and an unnoticed pause is the calmer failure.
    denial_line = _DENIAL_KIND_MARKER in raw_line
    if not denial_line and _TOOL_RESULT_MARKER in raw_line:
        state.auto_denials_consecutive = 0

    # Marker pre-filtering skips irrelevant lines cheaply - except while the
    # first prompt is still unknown, when user entries must be inspected too.
    interesting = (
        denial_line
        or _USAGE_MARKER in raw_line
        or _AI_TITLE_MARKER in raw_line
        or _CUSTOM_TITLE_MARKER in raw_line
        or _PERMISSION_MODE_MARKER in raw_line
        or _WORKTREE_STATE_MARKER in raw_line
        or _COMPACT_BOUNDARY_MARKER in raw_line
        or _SEARCH_COUNT_MARKER in raw_line
    )
    if not interesting and (state.first_prompt is not None or _USER_MARKER not in raw_line):
        return

    entry = _load(raw_line.decode('utf-8', errors='ignore'))
    if entry is None:
        return

    # The permission mode is recorded in two shapes, and a session can write only
    # one of them: the CLI logs a switch as its own ``permission-mode`` entry,
    # while a VS Code session never writes that entry at all and instead stamps
    # the mode in force onto each ordinary entry.  Both say the same thing about
    # the same point in the file, so reading the field wherever it appears - last
    # one wins - covers either shape without a per-entrypoint rule.  Sidechain
    # turns are skipped like everywhere else: a subagent's entries do not report
    # the main conversation's mode.
    if entry.get('isSidechain') is not True:
        permission_mode = entry.get('permissionMode')
        if isinstance(permission_mode, str) and permission_mode:
            state.permission_mode = permission_mode

    # A classifier block, read from the denial's own control field.  Sidechain
    # entries are skipped as everywhere else: a subagent's refusal is not the
    # main conversation's streak.  Every other denial kind - a rejection by the
    # user, a deny rule, a cancelled call - neither counts nor resets, matching
    # the documented rule that only an *allowed* action clears the streak.
    if entry.get('toolDenialKind') == _AUTO_MODE_DENIAL_KIND and entry.get('isSidechain') is not True:
        state.auto_denials_consecutive += 1
        state.auto_denials_total += 1

    entry_type = entry.get('type')

    if entry_type == 'user':
        state.web_searches += _search_count(entry)

    if entry_type == 'assistant':
        message = entry.get('message')
        _note_edited_files(message.get('content') if isinstance(message, dict) else None, state.edited_files)
        usage = message.get('usage') if isinstance(message, dict) else None
        if isinstance(usage, dict):
            usage = _unclaimed_usage(message, usage, state)
            # Sum into the overall totals always, and into a per-model bucket for
            # real models only. The synthetic sentinel (locally-generated turns,
            # zero usage) is not a real model - bucketing it would put an
            # unpriceable key in usage_by_model and wrongly drop the whole
            # session's cost to a plain token total.
            model = message.get('model') if isinstance(message, dict) else None
            buckets = [state.totals]
            if model != _SYNTHETIC_MODEL:
                model_key = model if isinstance(model, str) and model else ''
                buckets.append(state.by_model.setdefault(model_key, {key: 0 for key in _USAGE_TOTAL_KEYS}))
                skill = entry.get('attributionSkill')
                if isinstance(skill, str) and _SKILL_NAME_PATTERN.fullmatch(skill):
                    skill_models = state.by_skill.setdefault(skill, {})
                    buckets.append(skill_models.setdefault(model_key, {key: 0 for key in _USAGE_TOTAL_KEYS}))

            for key in _USAGE_KEYS:
                value = usage.get(key)
                for bucket in buckets:
                    _add_usage(bucket, key, value)

            creation = usage.get('cache_creation')
            if isinstance(creation, dict):
                for nested_key, total_key in _CACHE_TTL_KEYS.items():
                    value = creation.get(nested_key)
                    for bucket in buckets:
                        _add_usage(bucket, total_key, value)

            # Record each assistant turn in the MAIN conversation as an event for
            # the two switch logs (see _MODEL_KEYS / _CLI_KEYS). Ordering is
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
                # The writer goes in beside the version (see _CLI_KEYS); a
                # missing or mistyped entrypoint is stored as '' and left out
                # of the reported entry.
                entrypoint = entry.get('entrypoint')
                state.cli_events.append((timestamp, version, entrypoint if isinstance(entrypoint, str) else ''))

    elif entry_type == 'ai-title':
        value = entry.get('aiTitle')
        if isinstance(value, str) and value:
            state.ai_title = value

    elif entry_type == 'custom-title':
        value = entry.get('customTitle')
        if isinstance(value, str) and value:
            state.custom_title = value

    elif entry_type == _WORKTREE_STATE_TYPE:
        # An explicit null is the session leaving its worktree; a missing field or
        # a value of any other unexpected shape says nothing and leaves the
        # previous one.
        if 'worktreeSession' in entry and entry['worktreeSession'] is None:
            state.worktree = None
        else:
            worktree = _worktree_state(entry.get('worktreeSession'))
            if worktree is not None:
                state.worktree = worktree

    elif entry_type == 'system' and entry.get('subtype') == _COMPACT_BOUNDARY_SUBTYPE:
        if entry.get('isSidechain') is not True:
            state.compactions += 1

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


def _search_count(entry: dict) -> int:
    """Return how many web searches the tool result on *entry* made, or 0; nothing else of the result is read."""
    result = entry.get('toolUseResult')
    count = result.get('searchCount') if isinstance(result, dict) else None
    return count if _is_count(count) else 0


def _note_edited_files(content: object, edited_files: set[str]) -> None:
    """Add the file each file-writing tool call in *content* names to *edited_files*; nothing else of the call is read."""
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict) or block.get('type') != 'tool_use':
            continue
        path_key = _FILE_EDIT_TOOLS.get(block.get('name'))
        tool_input = block.get('input')
        if path_key is None or not isinstance(tool_input, dict):
            continue
        path = tool_input.get(path_key)
        if isinstance(path, str) and path:
            edited_files.add(path)


def _worktree_state(session: object) -> WorktreeState | None:
    """Parse a ``worktreeSession`` value, or None when it lacks a worktree path or a launch directory."""
    if not isinstance(session, dict):
        return None

    path = session.get('worktreePath')
    launch_cwds = tuple(
        value for value in (session.get('originalCwd'), session.get('preEnterOriginalCwd'))
        if isinstance(value, str) and value
    )
    if not isinstance(path, str) or not path or not launch_cwds:
        return None

    branch = session.get('worktreeBranch')
    return WorktreeState(launch_cwds=launch_cwds, path=path, branch=branch if isinstance(branch, str) and branch else None)


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
            display = _clip(f'{command_name} {args}' if args else command_name, _TITLE_MAX_CHARS)

            return display, command_name in _HOUSEKEEPING_TITLE_COMMANDS

    cleaned = ' '.join(_WRAPPER_PATTERN.sub('', text).split())
    if not cleaned:
        return None, False

    return _clip(cleaned, _TITLE_MAX_CHARS), False


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
