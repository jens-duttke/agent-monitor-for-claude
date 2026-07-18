"""
Subagents
=========

Counts the subagents a session is currently running and how many recently
finished, from the per-session subagent transcripts Claude Code writes under
``projects/<slug>/<session>/subagents/`` (directly, and nested under
``workflows/<wf>/`` for workflow-spawned agents).

A subagent within the active window is running until its transcript shows a
completed, settled turn (see ``_is_finished``); older ones have long finished.
Only control fields are read - file timestamps, the transcript tail's entry
``type``/``stop_reason``/block ``type``, and each ``meta.json``'s two display
fields (``agentType``, ``description``) - never the subagent's own
conversation content.

For a workflow (``workflows/<run>/``) the per-agent files leave a gap between
fan-out phases where no single agent is momentarily running, which would make
the whole workflow flicker between "your turn" and "working". So each run's
``journal.jsonl`` is read as a workflow-level signal: it records one ``started``
and one ``result`` event per agent, giving both the run's total agent count and
whether it is still active. Only each event's ``type`` and ``agentId`` decide the
counts; a ``result`` event's payload (the agent's returned content) is never
accessed or surfaced.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import cwd_to_slug, projects_dir
from .settings import SUBAGENT_RECENT_SECONDS

__all__ = ['SubagentInfo', 'WorkflowActivity', 'count_subagents']

_SUBAGENT_TAIL_BYTES = 65536

# Stop reasons that end an assistant turn cleanly on its own, with no tool owed
# and no continuation expected - treated as finished immediately. ``tool_use`` is
# deliberately absent (a workflow agent's final act is typically a
# ``StructuredOutput`` tool call, so its last turn's stop_reason is ``tool_use``
# even though the agent is done); so are reasons the harness may resume from
# (``max_tokens``, ``pause_turn``) - those fall to the quiet-settle path below
# instead of being claimed done while a continuation could still arrive.
_DONE_STOP_REASONS = frozenset({'end_turn', 'stop_sequence'})

# A transcript whose last turn was answered (a resolved tool call) but did not
# end on a natural stop counts as finished only once the file has been quiet this
# long, so a normal think/tool pause between a tool_result and the next turn is
# not misread as done. Kept well below ``SUBAGENT_RECENT_SECONDS``.
_SUBAGENT_SETTLE_SECONDS = 30

# A workflow with no open agent (every started one has returned) still counts as
# active while its journal was written this recently, bridging the orchestration
# pause between two fan-out phases when no single agent is momentarily running.
_WORKFLOW_GRACE_SECONDS = 30


@dataclass(frozen=True)
class WorkflowActivity:
    """Journal-derived activity for one background workflow run."""

    run_id: str
    total: int
    done: int
    active: bool


@dataclass(frozen=True)
class SubagentInfo:
    """Subagent activity for one session."""

    running: int = 0
    recent_done: int = 0
    labels: tuple[str, ...] = ()
    workflows: tuple[WorkflowActivity, ...] = ()


def count_subagents(session_id: str, cwd: str) -> SubagentInfo:
    """Return running/recently-finished subagent counts and running labels."""
    directory = _subagents_dir(session_id, cwd)
    if directory is None:
        return SubagentInfo()

    now = time.time()
    running_paths: list[Path] = []
    recent_done = 0

    # rglob walks lazily, so an inaccessible sub-directory raises mid-iteration -
    # materialize under a guard so a permission error degrades to "no subagents"
    # rather than propagating out and dropping the whole session.
    try:
        agent_paths = list(directory.rglob('agent-*.jsonl'))
    except OSError:
        return SubagentInfo()

    for path in agent_paths:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue

        # Older than the recent window: definitely long finished, skip.
        if age > SUBAGENT_RECENT_SECONDS:
            continue

        # A subagent is running while it is executing a tool or actively
        # producing; it is finished once its turn is complete and quiet.
        if _is_finished(path, age):
            recent_done += 1
        else:
            running_paths.append(path)

    labels = tuple(label for label in (_label(path) for path in running_paths) if label)
    workflows = _workflow_activity(directory, now)

    return SubagentInfo(running=len(running_paths), recent_done=recent_done, labels=labels, workflows=workflows)


def _is_finished(agent_path: Path, age: float) -> bool:
    """Return True if the subagent's transcript shows a completed, settled turn.

    A subagent is still running while it is executing a tool (its last entry is a
    ``tool_use`` awaiting its result) or actively producing (a freshly-written,
    non-terminal turn). It is finished once it ended on a natural stop, or its
    final tool call was answered and the transcript has gone quiet for
    ``_SUBAGENT_SETTLE_SECONDS``.

    Keying "finished" on a trailing ``end_turn`` alone (the previous behaviour)
    left completed agents stuck as running, because Claude Code does not always
    write one - a workflow agent's last act is typically a ``StructuredOutput``
    tool call whose ``tool_result`` is the final entry. Reads only the tail; an
    unreadable file is treated as finished so it never counts as running.
    """
    try:
        with agent_path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _SUBAGENT_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return True

    last = _last_entry(data)
    if last is None:
        return age >= _SUBAGENT_SETTLE_SECONDS

    stop_reason, executing_tool = last
    if executing_tool:
        return False
    if stop_reason in _DONE_STOP_REASONS:
        return True
    return age >= _SUBAGENT_SETTLE_SECONDS


def _last_entry(data: bytes) -> tuple[str | None, bool] | None:
    """Parse the last well-formed JSONL entry in *data*.

    Returns ``(stop_reason, executing_tool)`` - ``executing_tool`` is True when
    the last entry is an assistant turn whose content carries a ``tool_use``
    block, meaning nothing follows it yet, so the call is still awaiting its
    result. Returns None if no line parses. Only control fields are read; the
    subagent's conversation content is never returned.
    """
    for raw in reversed(data.split(b'\n')):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue

        message = entry.get('message')
        message = message if isinstance(message, dict) else {}
        stop_reason = message.get('stop_reason')
        content = message.get('content')
        if content is None:
            content = entry.get('content')

        has_tool_use = isinstance(content, list) and any(
            isinstance(block, dict) and block.get('type') == 'tool_use' for block in content
        )
        executing_tool = has_tool_use and entry.get('type') == 'assistant'

        return stop_reason if isinstance(stop_reason, str) else None, executing_tool

    return None


def _workflow_activity(subagents_dir: Path, now: float) -> tuple[WorkflowActivity, ...]:
    """Return per-workflow activity from each run's ``journal.jsonl``.

    A workflow counts as active while it still has agents that started but have
    not returned, or while its journal was written within the grace window (the
    orchestration pause between fan-out phases, when no single agent is
    momentarily running). A run whose journal has been quiet longer than the
    recent window is long finished and dropped. Only each event's ``type`` and
    ``agentId`` decide the counts; a ``result`` payload is never accessed.
    """
    workflows_root = subagents_dir / 'workflows'

    try:
        run_dirs = [entry for entry in workflows_root.iterdir() if entry.is_dir()]
    except OSError:
        return ()

    activities: list[WorkflowActivity] = []
    for run_dir in run_dirs:
        journal = run_dir / 'journal.jsonl'

        try:
            journal_age = now - journal.stat().st_mtime
        except OSError:
            continue

        if journal_age > SUBAGENT_RECENT_SECONDS:
            continue

        total, done = _journal_counts(journal)
        if total == 0:
            continue

        active = total > done or journal_age < _WORKFLOW_GRACE_SECONDS
        activities.append(WorkflowActivity(run_id=run_dir.name, total=total, done=done, active=active))

    return tuple(activities)


# {journal path: (mtime_ns, size, (started, done))} - the append-only journal is
# re-parsed only when it grew. Bounded so a long-lived monitor cannot grow it
# without limit.
_journal_cache: dict[str, tuple[int, int, tuple[int, int]]] = {}
_JOURNAL_CACHE_MAX = 64


def _journal_counts(journal_path: Path) -> tuple[int, int]:
    """Return ``(started, done)`` distinct-agent counts from a workflow journal.

    Each line's ``type`` and ``agentId`` decide the counts; a ``result`` line's
    payload (the agent's returned content) is parsed away with the line but never
    accessed or surfaced. A malformed line is skipped.

    The journal is append-only, so the parse is cached by its size and mtime: a
    poll where nothing was appended reuses the previous counts instead of
    re-reading the whole file (which, for a large run, is dominated by the very
    ``result`` payloads the counts do not need).
    """
    try:
        stat_result = journal_path.stat()
    except OSError:
        return 0, 0

    key = str(journal_path)
    cached = _journal_cache.get(key)
    if cached is not None and cached[0] == stat_result.st_mtime_ns and cached[1] == stat_result.st_size:
        return cached[2]

    started: set[str] = set()
    done: set[str] = set()
    try:
        with journal_path.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(entry, dict):
                    continue

                agent_id = entry.get('agentId')
                if not isinstance(agent_id, str) or not agent_id:
                    continue

                kind = entry.get('type')
                if kind == 'started':
                    started.add(agent_id)
                elif kind == 'result':
                    done.add(agent_id)
    except OSError:
        return 0, 0

    counts = (len(started), len(done))
    while len(_journal_cache) >= _JOURNAL_CACHE_MAX:
        _journal_cache.pop(next(iter(_journal_cache)))
    _journal_cache[key] = (stat_result.st_mtime_ns, stat_result.st_size, counts)
    return counts


def _subagents_dir(session_id: str, cwd: str) -> Path | None:
    """Return the session's subagents directory, or None if it does not exist."""
    if not session_id or not cwd:
        return None

    directory = projects_dir() / cwd_to_slug(cwd) / session_id / 'subagents'
    return directory if directory.is_dir() else None


def _label(agent_path: Path) -> str:
    """Return a running subagent's display label from its ``meta.json``.

    Prefers the ``description`` (what the subagent was asked to do), falling
    back to the ``agentType``.  Only these two fields are read.
    """
    meta_path = agent_path.parent / (agent_path.stem + '.meta.json')

    try:
        data = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return ''

    if not isinstance(data, dict):
        return ''

    description = data.get('description')
    if isinstance(description, str) and description:
        return description

    agent_type = data.get('agentType')
    if isinstance(agent_type, str) and agent_type:
        return agent_type

    return ''
