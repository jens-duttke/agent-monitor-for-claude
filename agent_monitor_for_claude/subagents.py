"""
Subagents
=========

Counts the subagents a session is currently running and how many recently
finished, from the per-session subagent transcripts Claude Code writes under
``projects/<slug>/<session>/subagents/`` (directly, and nested under
``workflows/<wf>/`` for workflow-spawned agents).

A subagent within the active window is running until its transcript shows a
completed, settled turn (see ``_is_finished``); older ones have long finished.
The freshest running agent's file age is reported alongside the counts
(``running_age``), because a session that delegates a whole turn writes nothing
to its own transcript meanwhile and would otherwise appear to have gone quiet.
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

A session's cost includes its subagents: every agent transcript - running or
long finished - goes through the transcript module's usage scan
(:func:`~agent_monitor_for_claude.transcript.usage_scan`, which reads the
replies' usage and nothing of their content), and the sums are grouped by the
agent's ``agentType`` for the cost tooltip.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import SessionRoot, cwd_to_slug, projects_dir
from .settings import SUBAGENT_RECENT_SECONDS
from .transcript import agent_steps, final_reply, usage_scan, usage_scan_once
from .usage_parts import UsageScan, merge_scans

__all__ = ['AgentNode', 'SubagentInfo', 'WorkflowActivity', 'agent_texts', 'count_subagents', 'past_subagent_usage']

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


# Each listed agent's type, description and parent, by path: ``meta.json`` is
# written once when the agent starts.  pywebview answers each bridge call on
# its own thread, so overlapping snapshots share the cache; the lock keeps one
# from evicting while another iterates.
_node_meta_cache: dict[str, tuple[str, str, str]] = {}
_node_meta_lock = threading.Lock()
_NODE_META_CACHE_MAX = 20000
# The length of a listed agent's type and description.
_NODE_TEXT_MAX_CHARS = 160
# The agent transcripts found for an agent's details, by (origin, session, agent).
_agent_file_cache: dict[tuple[str, str, str], Path] = {}
_agent_file_lock = threading.Lock()
_AGENT_FILE_CACHE_MAX = 64
# An agent id as it appears in its file name - letters, digits, ``_`` and
# ``-`` only, so one can never name a path outside the agent's folder - and the
# longest first line read for its task.
_AGENT_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_PROMPT_LINE_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class WorkflowActivity:
    """Journal-derived activity for one background workflow run."""

    run_id: str
    total: int
    done: int
    active: bool


@dataclass(frozen=True)
class AgentNode:
    """One subagent the session ran within the recent window, for the connection view.

    ``agent_id`` is the id in its file name, which a hand-back message names as
    its sender; ``parent_id`` the agent that started it, when not the session
    itself; ``workflow`` the workflow run it belongs to, or ``''``.  ``skill``
    is the skill that ran as this agent (a forked skill has no description of
    its own) and ``usage_by_model`` what it spent, ``started_at`` when its first
    entry was written (POSIX seconds), ``tool_calls`` and the lines it changed -
    all from the usage scan its cost already needs, empty while that scan is
    pending.
    """

    agent_id: str
    agent_type: str
    description: str
    running: bool
    age: float
    parent_id: str = ''
    workflow: str = ''
    skill: str = ''
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    started_at: float | None = None
    tool_calls: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    tools: dict[str, tuple[int, float | None, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentInfo:
    """Subagent activity for one session."""

    running: int = 0
    recent_done: int = 0
    labels: tuple[str, ...] = ()
    workflows: tuple[WorkflowActivity, ...] = ()
    # Seconds since the most recently written *running* subagent's transcript, or
    # None when none is running.  A session whose whole turn happens inside a
    # subagent appends nothing to its own transcript for as long as that agent
    # works, so its activity age would grow while it is plainly busy; this is the
    # activity the main transcript cannot see.  Finished agents are excluded:
    # once one returns, the main transcript carries the turn again.
    running_age: float | None = None
    # The usage of every agent the session ran, summed per ``agentType`` (``''``
    # when its metadata names none), and whether any agent's first scan is still
    # running in the background - the sums then leave that agent out.
    usage_by_type: dict[str, UsageScan] = field(default_factory=dict)
    usage_pending: bool = False
    # Every agent of the recent window, running or finished, newest first.
    agents: tuple[AgentNode, ...] = ()


def count_subagents(root: SessionRoot, session_id: str, cwd: str, worktree_cwds: Iterable[str] = ()) -> SubagentInfo:
    """Return running/recently-finished subagent counts and running labels for a session under *root*, and their usage.

    A session that switched into a worktree writes the agents it starts there
    under the worktree's project folder, while its transcript and its earlier
    agents stay under *cwd*'s - so the folder of every one of *worktree_cwds*
    is read too.
    """
    directories = _subagents_dirs(root, session_id, cwd, worktree_cwds)
    if not directories:
        return SubagentInfo()

    now = time.time()
    running_paths: list[Path] = []
    running_age: float | None = None
    recent_done = 0
    candidates: list[tuple[float, Path, bool]] = []

    agent_paths: list[Path] = []
    for directory in directories:
        agent_paths.extend(_agent_paths(directory) or ())

    usage_by_type, usage_pending, usage_by_path = _usage_by_type(agent_paths, usage_scan)

    for path in agent_paths:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue

        # Older than the recent window: definitely long finished - only the
        # connection view still lists it (every agent the session ran; the view
        # narrows them to its time window itself).
        if age > SUBAGENT_RECENT_SECONDS:
            candidates.append((age, path, False))
            continue

        # A subagent is running while it is executing a tool or actively
        # producing; it is finished once its turn is complete and quiet.
        if _is_finished(path, age):
            recent_done += 1
            candidates.append((max(0.0, age), path, False))
            continue
        candidates.append((max(0.0, age), path, True))

        running_paths.append(path)
        # A clock skew that puts the file ahead of ``now`` would otherwise report
        # a negative age; clamp it the way _activity_age does.
        clamped_age = max(0.0, age)
        if running_age is None or clamped_age < running_age:
            running_age = clamped_age

    labels = tuple(label for label in (_label(path) for path in running_paths) if label)
    workflows = tuple(activity for directory in directories for activity in _workflow_activity(directory, now))
    newest = sorted(candidates, key=lambda item: item[0])

    return SubagentInfo(
        running=len(running_paths), recent_done=recent_done, labels=labels, workflows=workflows, running_age=running_age,
        usage_by_type=usage_by_type, usage_pending=usage_pending,
        agents=tuple(_agent_node(path, running, age, usage_by_path.get(path)) for age, path, running in newest),
    )


def _agent_node(path: Path, running: bool, age: float, usage: UsageScan | None) -> AgentNode:
    """Describe one agent file for the connection view from its file name, its folder, its ``meta.json`` and its *usage*."""
    key = str(path)
    with _node_meta_lock:
        cached = _node_meta_cache.get(key)
    if cached is None:
        meta = _meta(path)
        agent_type = meta.get('agentType') if isinstance(meta.get('agentType'), str) else ''
        description = meta.get('description') if isinstance(meta.get('description'), str) else ''
        parent = meta.get('parentAgentId') if isinstance(meta.get('parentAgentId'), str) else ''
        cached = (agent_type[:_NODE_TEXT_MAX_CHARS], description[:_NODE_TEXT_MAX_CHARS], parent)
        # Only a file that had its meta keeps the answer; one written a moment later is asked again.
        if meta:
            with _node_meta_lock:
                while len(_node_meta_cache) >= _NODE_META_CACHE_MAX:
                    _node_meta_cache.pop(next(iter(_node_meta_cache)))
                _node_meta_cache[key] = cached
    agent_id = path.stem[len('agent-'):] if path.stem.startswith('agent-') else path.stem
    workflow = path.parent.name if path.parent.parent.name == 'workflows' else ''

    if usage is None:
        return AgentNode(agent_id, cached[0], cached[1], running, age, cached[2], workflow)

    by_model = {model: dict(counts) for model, counts in usage.by_model.items()}
    return AgentNode(
        agent_id, cached[0], cached[1], running, age, cached[2], workflow, _main_skill(usage), by_model,
        usage.started_at, usage.tool_calls, usage.lines_added, usage.lines_removed, dict(usage.tools),
    )


def _main_skill(usage: UsageScan | None) -> str:
    """The skill most of the agent's output was written under, or ``''`` when none was."""
    best, best_output = '', 0
    for skill, by_model in (usage.by_skill.items() if usage is not None else ()):
        output = sum(counts.get('output_tokens', 0) for counts in by_model.values())
        if output > best_output:
            best, best_output = skill, output

    return best


def agent_texts(root: SessionRoot, session_id: str, agent_id: str) -> dict[str, Any] | None:
    """Return the task one of the session's subagents was given, its result and every step it took, all whole, or None when it cannot be found.

    The task is the first entry of the agent's own transcript - the prompt the
    session wrote for it, or a forked skill's instructions; the result is its
    newest reply - what the session receives once it finishes, its latest word
    while it runs; the steps are its tool calls with their inputs and results
    and its replies between them (see :func:`transcript.agent_steps`).  They
    are read only on request, for the agent the details show.  The agent may lie under any project folder (one the
    session switched into as a worktree files its agents there), so every
    folder is checked for the session's own ``subagents`` directory; the file
    found must resolve inside ``projects/``.  The caller validates
    *session_id*; *agent_id* is checked here.
    """
    path = _agent_file(root, session_id, agent_id)
    if path is None:
        return None

    return {'task': _first_prompt(path) or '', 'result': final_reply(path) or '', 'steps': agent_steps(path)}


def _agent_file(root: SessionRoot, session_id: str, agent_id: str) -> Path | None:
    """The transcript of one of the session's subagents under any project folder, confined to ``projects/``, or None.

    A running agent's details ask again on every write, so a path found is kept
    while its file exists; finding it means listing every project folder.
    """
    if not session_id or not isinstance(agent_id, str) or not _AGENT_ID_PATTERN.fullmatch(agent_id):
        return None

    key = (root.origin, session_id, agent_id)
    with _agent_file_lock:
        known = _agent_file_cache.get(key)
    if known is not None and known.is_file():
        return known

    found = _find_agent_file(projects_dir(root), session_id, agent_id)
    with _agent_file_lock:
        if found is None:
            _agent_file_cache.pop(key, None)
        else:
            _agent_file_cache[key] = found
            while len(_agent_file_cache) > _AGENT_FILE_CACHE_MAX:
                _agent_file_cache.pop(next(iter(_agent_file_cache)))

    return found


def _find_agent_file(projects: Path, session_id: str, agent_id: str) -> Path | None:
    """Search every project folder's ``<session>/subagents`` for the agent's transcript."""
    try:
        folders = [Path(entry.path) for entry in os.scandir(projects) if entry.is_dir()]
    except OSError:
        return None

    name = f'agent-{agent_id}.jsonl'
    for folder in folders:
        directory = folder / session_id / 'subagents'
        if not directory.is_dir():
            continue
        for path in _agent_paths(directory) or ():
            if path.name == name and _inside(path, projects):
                return path

    return None


def _inside(path: Path, directory: Path) -> bool:
    """Whether *path* resolves to a file inside *directory*."""
    try:
        path.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False

    return True


def _first_prompt(path: Path) -> str | None:
    """The text of the first entry of an agent's transcript, or None when it holds none."""
    try:
        with path.open('rb') as handle:
            line = handle.readline(_PROMPT_LINE_MAX_BYTES)
    except OSError:
        return None

    try:
        entry = json.loads(line.decode('utf-8', errors='replace'))
    except ValueError:
        return None
    message = entry.get('message') if isinstance(entry, dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None

    parts = [block.get('text') for block in content if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)]
    text = '\n\n'.join(part.strip() for part in parts if part.strip())
    return text or None


def past_subagent_usage(transcript: Path, worktree_cwds: Iterable[str] = ()) -> dict[str, UsageScan]:
    """Return the usage of a past session's subagents, summed per ``agentType``, each file read once.

    *transcript* is the session's own transcript; its agents lie in the folder
    of the same name, and those it started after switching into a worktree in
    the same-named folder under each of *worktree_cwds*' project folders - the
    layout :func:`count_subagents` reads for a live session.  A session
    without either has no subagents.
    """
    projects = transcript.parent.parent
    directories: list[Path] = []
    for directory in (transcript.parent / transcript.stem / 'subagents',
                      *(projects / cwd_to_slug(cwd) / transcript.stem / 'subagents' for cwd in worktree_cwds if cwd)):
        if directory not in directories and directory.is_dir():
            directories.append(directory)

    agent_paths: list[Path] = []
    for directory in directories:
        agent_paths.extend(_agent_paths(directory) or ())

    usage_by_type, _, _ = _usage_by_type(agent_paths, usage_scan_once)
    return usage_by_type


def _agent_paths(directory: Path) -> list[Path] | None:
    """Return every agent transcript below *directory*, or None if the folder cannot be walked.

    rglob walks lazily, so an inaccessible sub-directory raises mid-iteration -
    materialized under a guard so a permission error degrades to "no
    subagents" rather than propagating out and dropping the whole session.
    """
    try:
        return list(directory.rglob('agent-*.jsonl'))
    except OSError:
        return None


def _usage_by_type(
    agent_paths: Iterable[Path], scan: Callable[[Path], UsageScan | None],
) -> tuple[dict[str, UsageScan], bool, dict[Path, UsageScan]]:
    """Return the usage *scan* reads from each agent transcript, summed per ``agentType``, whether any scan was pending, and each one's own."""
    scans_by_type: dict[str, list[UsageScan]] = {}
    by_path: dict[Path, UsageScan] = {}
    pending = False
    for path in agent_paths:
        usage = scan(path)
        if usage is None:
            pending = True
            continue
        by_path[path] = usage
        scans_by_type.setdefault(_agent_type(path), []).append(usage)

    return {agent_type: merge_scans(scans) for agent_type, scans in scans_by_type.items()}, pending, by_path


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


def _subagents_dirs(root: SessionRoot, session_id: str, cwd: str, worktree_cwds: Iterable[str]) -> list[Path]:
    """Return the session's existing subagents directories under *root*: *cwd*'s, then each worktree's."""
    if not session_id or not cwd:
        return []

    directories: list[Path] = []
    for directory_cwd in (cwd, *worktree_cwds):
        if not directory_cwd:
            continue
        directory = projects_dir(root) / cwd_to_slug(directory_cwd) / session_id / 'subagents'
        if directory not in directories and directory.is_dir():
            directories.append(directory)

    return directories


def _label(agent_path: Path) -> str:
    """Return a running subagent's display label from its ``meta.json``.

    Prefers the ``description`` (what the subagent was asked to do), falling
    back to the ``agentType``.  Only these two fields are read.
    """
    data = _meta(agent_path)

    description = data.get('description')
    if isinstance(description, str) and description:
        return description

    agent_type = data.get('agentType')
    if isinstance(agent_type, str) and agent_type:
        return agent_type

    return ''


# An ``agentType`` as Claude Code writes it (``next:reviewer``, ``Explore``); a
# name of any other shape is summed under no type rather than shown verbatim.
_AGENT_TYPE_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,63}')

# {agent transcript path: agentType} - the metadata never changes once written,
# so each agent's file is read once, not on every poll.  A failed read is not
# cached: the metadata can land a moment after the transcript.  Bounded so a
# long-lived monitor cannot grow it without limit.
_agent_type_cache: dict[str, str] = {}
_AGENT_TYPE_CACHE_MAX = 4096


def _agent_type(agent_path: Path) -> str:
    """Return the ``agentType`` an agent's ``meta.json`` names, or ``''``; nothing else of the file is kept."""
    key = str(agent_path)
    cached = _agent_type_cache.get(key)
    if cached is not None:
        return cached

    data = _meta(agent_path)
    agent_type = data.get('agentType')
    if not isinstance(agent_type, str) or not _AGENT_TYPE_PATTERN.fullmatch(agent_type):
        return ''

    while len(_agent_type_cache) >= _AGENT_TYPE_CACHE_MAX:
        _agent_type_cache.pop(next(iter(_agent_type_cache)))
    _agent_type_cache[key] = agent_type
    return agent_type


def _meta(agent_path: Path) -> dict:
    """Return the agent's sibling ``agent-<id>.meta.json`` as a dict, or an empty dict if it cannot be read."""
    meta_path = agent_path.parent / (agent_path.stem + '.meta.json')

    try:
        data = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    return data if isinstance(data, dict) else {}
