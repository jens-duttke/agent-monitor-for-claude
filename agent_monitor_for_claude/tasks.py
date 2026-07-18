"""
Background Task Output
======================

Lists and reads the output files Claude Code keeps for a session's background
tasks (a ``Bash`` tool run with ``run_in_background``).  Claude Code streams
each such task's stdout/stderr live to
``<temp>/claude/<project-slug>/<session-id>/tasks/<task-id>.output`` and tells
the model to ``Read`` that file for interim output, so this is the one place
where a still-running process's progress is observable on disk.

This module reads that process output - real content, not just metadata - so it
is deliberately encapsulated (see the ``search`` module for the same discipline
on the content boundary): the output text is read only on an explicit,
user-initiated expand.  Each task is also labelled with the description (or
command) the agent gave the ``run_in_background`` call, read from the session
transcript - again process content, not conversation, and shown so a task row
says what it is instead of an opaque id.

When a task's own capture file is empty because the command redirected its
output elsewhere (``... > run.log 2>&1``), the redirect target parsed from that
command is read instead - but only when it resolves **inside the session's own
scratchpad or project directory**, so a redirect can never make this read a file
outside those two session-owned trees.  Every path is resolved and checked with
``relative_to`` against its allowed root, and both the session id (a UUID) and
the task id are validated before any file is touched.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import scratchpad_dir, task_output_dir, task_output_path, transcript_path

__all__ = ['TaskInfo', 'list_tasks', 'read_task_output']

_SESSION_ID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_TASK_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

# The registration line Claude Code writes as the tool result of a background
# task, naming the task id.  Matched to map an id back to the command that
# started it (see ``_task_meta``).
_TASK_REGISTER_PATTERN = re.compile(r'ID:\s*([A-Za-z0-9_-]+)\.\s*Output is being written to')

# A shell output redirect: an optional fd (or ``&``) then ``>``/``>>`` then the
# target (quoted or a bareword).  Used to find where a task sent its output when
# its own capture file stays empty.
_REDIRECT_PATTERN = re.compile(r'(?P<fd>\d*|&)>>?\s*(?P<target>"[^"]*"|\'[^\']*\'|[^\s;|&<>]+)')

# fds whose redirect target receives stdout (so it holds the output we want):
# none (``>``), ``1`` (``1>``), or ``&`` (``&>``, both streams).  ``2>`` is
# stderr-only and ignored.
_STDOUT_FDS = frozenset({'', '1', '&'})

# Longest label kept; a command line can be long, so it is trimmed (the UI also
# ellipsizes).  A description is usually short and used as-is.
_LABEL_MAX_LEN = 140

# Windows drive letters WSL maps under /mnt, e.g. /mnt/c -> C:\.
_WSL_MOUNT_PATTERN = re.compile(r'^/mnt/([A-Za-z])(/.*)?$')

# Freshest N tasks shown; the rest are reported as a dropped count rather than
# silently hidden.  Ordering by most-recent write keeps a running task (whose
# output is still growing) at the top, so the cap never hides it.
_DEFAULT_MAX_TASKS = 25

# Only the tail of an output file is read: a live console cares about where the
# process is now, and a runaway log must never be loaded whole.
_OUTPUT_TAIL_BYTES = 65536


@dataclass(frozen=True)
class TaskInfo:
    """One background task: output-file metadata plus its human label."""

    task_id: str
    size_bytes: int
    age_seconds: float
    label: str


def list_tasks(
    session_id: str,
    cwd: str,
    *,
    max_tasks: int = _DEFAULT_MAX_TASKS,
    recent_seconds: float | None = None,
) -> tuple[list[TaskInfo], int]:
    """Return a session's recent background tasks and the total found.

    File metadata (name, size, last-write age) comes from the task directory;
    each task's label (the description or command from its ``run_in_background``
    call) is read from the session transcript.

    Parameters
    ----------
    session_id : str
        The session UUID (validated; a non-UUID yields no tasks).
    cwd : str
        The session working directory, mapped to the project slug.
    max_tasks : int
        Cap on the returned list; the second tuple element reports how many
        recent tasks existed in total, so the UI can flag a truncated view.
    recent_seconds : float or None
        Skip tasks whose output has been idle longer than this; ``None`` keeps
        all of them.

    Returns
    -------
    tuple[list[TaskInfo], int]
        The newest-first tasks (freshest write first) and the total recent count.
    """
    if not _valid_session(session_id) or not isinstance(cwd, str) or not cwd:
        return [], 0

    directory = task_output_dir(session_id, cwd)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return [], 0

    task_ids = [entry.stem for entry in entries if entry.suffix == '.output' and _TASK_ID_PATTERN.match(entry.stem)]
    if not task_ids:
        return [], 0

    meta = _task_meta(session_id, cwd)
    now = time.time()
    infos: list[TaskInfo] = []
    for task_id in task_ids:
        command = meta.get(task_id, {}).get('command', '')
        # Size and age describe the file actually shown - the redirect target
        # when the capture file is empty, so the row does not read "0 B" while
        # its output lives elsewhere.  None means the capture path escaped its
        # directory (a symlink) and is refused, so the task is skipped.
        path = _effective_output_path(session_id, cwd, task_id, command)
        if path is None:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue

        age = max(0.0, now - stat_result.st_mtime)
        if recent_seconds is not None and age > recent_seconds:
            continue

        label = meta.get(task_id, {}).get('label', '')
        infos.append(TaskInfo(task_id=task_id, size_bytes=int(stat_result.st_size), age_seconds=age, label=label))

    infos.sort(key=lambda task: task.age_seconds)
    total = len(infos)
    return infos[:max_tasks], total


def read_task_output(session_id: str, cwd: str, task_id: str, *, max_bytes: int = _OUTPUT_TAIL_BYTES) -> str | None:
    """Return the tail of one background task's output, or ``None``.

    Reads the task's own capture file, or - when that is empty because the
    command redirected its output - the redirect target, but only if it resolves
    inside the session's scratchpad or project directory.  Reads at most the last
    ``max_bytes`` and drops the truncated leading line, marking the cut with an
    ellipsis.  Returns ``None`` when an id is invalid or the file cannot be read.
    """
    if not _valid_session(session_id) or not isinstance(cwd, str) or not cwd:
        return None
    if not isinstance(task_id, str) or not _TASK_ID_PATTERN.match(task_id):
        return None

    command = _task_meta(session_id, cwd).get(task_id, {}).get('command', '')
    path = _effective_output_path(session_id, cwd, task_id, command)
    if path is None:
        return None
    return _read_tail(path, max_bytes)


def _read_tail(path: Path, max_bytes: int) -> str | None:
    """Return the last ``max_bytes`` of *path* as text, or ``None`` if unreadable."""
    try:
        size = path.stat().st_size
        with open(path, 'rb') as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return None

    text = data.decode('utf-8', errors='replace')
    if size > max_bytes:
        newline = text.find('\n')
        if newline != -1:
            text = text[newline + 1:]
        text = '…\n' + text

    return text


def _effective_output_path(session_id: str, cwd: str, task_id: str, command: str) -> Path | None:
    """Return the file to read for a task: its capture file, else the redirect target.

    The capture file wins whenever it has content.  Only when it is empty (the
    command redirected output away) is the redirect target used, and only if it
    stays inside the session's own scratchpad or project directory.  Returns
    ``None`` when the capture path resolves outside the tasks directory (a
    symlinked ``<id>.output``) - a confinement failure must refuse, never read
    the escaped target.
    """
    base = task_output_path(session_id, cwd, task_id)
    try:
        base = base.resolve()
        base.relative_to(task_output_dir(session_id, cwd).resolve())
    except (OSError, ValueError):
        return None

    try:
        if base.is_file() and base.stat().st_size > 0:
            return base
    except OSError:
        return base

    redirect = _resolve_redirect(command, session_id, cwd)
    return redirect if redirect is not None else base


def _resolve_redirect(command: str, session_id: str, cwd: str) -> Path | None:
    """Return the command's output-redirect file if it is a real, in-bounds file.

    The target is confined to the session's scratchpad or its project directory;
    a WSL ``/mnt/<drive>/`` target is translated to its Windows path first.  Any
    target outside both roots, or that is not an existing file, yields ``None``.
    """
    target = _parse_redirect_target(command)
    if not target:
        return None

    # A relative target was written relative to the task's working directory;
    # resolve it against the session cwd (best effort - the confinement check
    # below still gates it) rather than the monitor's own cwd.
    candidate = Path(_wsl_to_windows(target))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None

    roots = (scratchpad_dir(session_id, cwd), Path(cwd))
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
        return None

    return None


def _parse_redirect_target(command: str) -> str | None:
    """Return the last stdout-redirect target in *command*, unquoted, or ``None``."""
    if not isinstance(command, str) or not command:
        return None

    target: str | None = None
    for match in _REDIRECT_PATTERN.finditer(command):
        if match.group('fd') not in _STDOUT_FDS:
            continue
        raw = match.group('target')
        if raw.startswith(('"', "'")):
            raw = raw[1:-1]
        else:
            raw = raw.strip('"\'')
        if raw:
            target = raw

    return target


def _wsl_to_windows(path: str) -> str:
    """Translate a WSL ``/mnt/<drive>/...`` path to Windows form; pass others through."""
    match = _WSL_MOUNT_PATTERN.match(path)
    if not match:
        return path

    drive = match.group(1).upper()
    rest = (match.group(2) or '').replace('/', '\\')
    return f'{drive}:{rest}' if rest else f'{drive}:\\'


def _valid_session(session_id: str) -> bool:
    """Return True if *session_id* is a well-formed UUID."""
    return isinstance(session_id, str) and bool(_SESSION_ID_PATTERN.match(session_id))


# {transcript path: (mtime_ns, size, {task_id: {'label', 'command'}})} - so the
# panel's repeated refresh re-parses the transcript only when it actually grew.
# Bounded so a long-lived monitor visiting many sessions cannot grow it forever.
_meta_cache: dict[str, tuple[int, int, dict[str, dict[str, str]]]] = {}
_META_CACHE_MAX = 32


def _task_meta(session_id: str, cwd: str) -> dict[str, dict[str, str]]:
    """Return ``{task_id: {'label', 'command'}}`` from the transcript's background calls.

    The label is the ``description`` (or, failing that, the ``command``) the
    agent passed to the ``run_in_background`` Bash call; the command is kept so a
    redirected output file can be found.  Both are joined to the task id via the
    registration line Claude Code writes as that call's result.  Only lines that
    could carry either are JSON-parsed, so even a large transcript is cheap.
    """
    path = transcript_path(session_id, cwd)
    try:
        stat_result = path.stat()
    except OSError:
        return {}

    key = str(path)
    cached = _meta_cache.get(key)
    if cached is not None and cached[0] == stat_result.st_mtime_ns and cached[1] == stat_result.st_size:
        return cached[2]

    uses: dict[str, dict[str, str]] = {}   # tool_use id -> {'label', 'command'}
    task_to_use: dict[str, str] = {}       # task id -> tool_use id
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if 'run_in_background' not in line and 'Output is being written to' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                _scan_meta_entry(entry, uses, task_to_use)
    except OSError:
        return {}

    meta = {}
    for task_id, use_id in task_to_use.items():
        info = uses.get(use_id)
        if info:
            meta[task_id] = info

    while len(_meta_cache) >= _META_CACHE_MAX:
        _meta_cache.pop(next(iter(_meta_cache)))
    _meta_cache[key] = (stat_result.st_mtime_ns, stat_result.st_size, meta)
    return meta


def _scan_meta_entry(entry: object, uses: dict[str, dict[str, str]], task_to_use: dict[str, str]) -> None:
    """Collect background-task label/command and id mappings from one transcript entry."""
    message = entry.get('message') if isinstance(entry, dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue

        kind = block.get('type')
        if kind == 'tool_use' and block.get('name') == 'Bash':
            info = _meta_from_input(block.get('input'))
            if info:
                uses[str(block.get('id'))] = info
        elif kind == 'tool_result':
            text = block.get('content')
            if isinstance(text, str):
                match = _TASK_REGISTER_PATTERN.search(text)
                if match:
                    task_to_use[match.group(1)] = str(block.get('tool_use_id'))


def _meta_from_input(tool_input: object) -> dict[str, str] | None:
    """Return ``{'label', 'command'}`` from a Bash tool input, or ``None``."""
    if not isinstance(tool_input, dict):
        return None

    command = tool_input.get('command')
    command = command.strip() if isinstance(command, str) else ''

    description = tool_input.get('description')
    if isinstance(description, str) and description.strip():
        label = description.strip()[:_LABEL_MAX_LEN]
    else:
        label = command[:_LABEL_MAX_LEN]

    if not label and not command:
        return None

    return {'label': label, 'command': command}
