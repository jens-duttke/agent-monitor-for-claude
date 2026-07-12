"""
Subagents
=========

Counts the subagents a session is currently running and how many recently
finished, from the per-session subagent transcripts Claude Code writes under
``projects/<slug>/<session>/subagents/`` (directly, and nested under
``workflows/<wf>/`` for workflow-spawned agents).

A subagent whose transcript was written within the active window is running;
older ones have finished.  Only file timestamps and the two display fields of
each subagent's ``meta.json`` (``agentType``, ``description``) are read -
never the subagent's own conversation content.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import cwd_to_slug, projects_dir
from .settings import SUBAGENT_RECENT_SECONDS

__all__ = ['SubagentInfo', 'count_subagents']

# A subagent transcript ends with the final assistant turn's end_turn; the
# last non-null stop_reason tells running (tool_use / none) from done.
_STOP_REASON_PATTERN = re.compile(rb'"stop_reason":\s*"([a-z_]+)"')
_SUBAGENT_TAIL_BYTES = 16384


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

        # A subagent is running until its transcript ends with end_turn -
        # freshness alone would drop it during its own think/tool pauses.
        if _is_finished(path):
            recent_done += 1
        else:
            running_paths.append(path)

    labels = tuple(label for label in (_label(path) for path in running_paths) if label)

    return SubagentInfo(running=len(running_paths), recent_done=recent_done, labels=labels)


def _is_finished(agent_path: Path) -> bool:
    """Return True if the subagent's transcript ends with a finished turn.

    Reads only the tail and scans for the last non-null ``stop_reason``; an
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

    matches = _STOP_REASON_PATTERN.findall(data)
    return bool(matches) and matches[-1] == b'end_turn'


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
