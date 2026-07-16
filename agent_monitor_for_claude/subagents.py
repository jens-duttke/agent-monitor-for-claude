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
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import cwd_to_slug, projects_dir
from .settings import SUBAGENT_RECENT_SECONDS

__all__ = ['SubagentInfo', 'count_subagents']

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


@dataclass(frozen=True)
class SubagentInfo:
    """Subagent activity for one session."""

    running: int = 0
    recent_done: int = 0
    labels: tuple[str, ...] = ()


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

    return SubagentInfo(running=len(running_paths), recent_done=recent_done, labels=labels)


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
