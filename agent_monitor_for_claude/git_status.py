"""
Git status
==========

What a session's working directory holds that is not committed yet: the
changed and untracked files, the lines they add and remove against ``HEAD``,
the branch's distance to its upstream, and which of the files the session
itself edited are among them - and, for one of those files on request, its
diff.

This module runs the ``git`` executable - with fixed arguments, a working
directory that must be an existing directory, a timeout and no window - and
only when the UI asks for the selected session.  Nothing is written: the
optional index refresh ``git status`` may perform is switched off through
``GIT_OPTIONAL_LOCKS=0``, and ``safe.directory`` is overridden for this one
call only, so a repository owned by another user (a WSL distro's, reached over
``\\\\wsl.localhost``) can still be read.  The executable is looked up on
``PATH`` alone, never in the current directory.  A diff never runs an external
diff or text-conversion program a repository may configure.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import platforms

__all__ = ['git_diff', 'git_status']

# How long a result is reused for the same directory and edited files; the UI
# asks again while the session stays selected.
_CACHE_SECONDS = 4.0
# Each git call is given this long before the status is reported unknown.
_TIMEOUT_SECONDS = 8.0
# The changed files listed by name; the counts cover all of them.
_MAX_FILES = 60
# A file's diff is cut after this many bytes, at a line end.
_MAX_DIFF_BYTES = 256 * 1024
# The longest file path a diff is asked for.
_MAX_PATH_CHARS = 4096
# Options every diff carries: no colour, and no program the repository's
# configuration names for diffing or converting a file.
_DIFF_OPTIONS = ('--no-color', '--no-ext-diff', '--no-textconv')

_cache: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()


def git_status(directory: str, edited: Iterable[str] = ()) -> dict[str, Any] | None:
    """Describe the uncommitted state of the repository holding *directory*.

    Parameters
    ----------
    directory : str
        A directory this host can open (a WSL path already translated).
    edited : Iterable[str]
        Paths of the files the session edited, in the same host form; the
        result says which of them are still uncommitted.

    Returns
    -------
    dict or None
        ``{'branch', 'upstream', 'ahead', 'behind', 'changed', 'untracked',
        'added', 'removed', 'files', 'edited_total', 'edited_uncommitted'}``,
        or None when *directory* is no directory, lies in no repository, or git
        is not installed or does not answer in time.
    """
    if not isinstance(directory, str) or not directory or not os.path.isdir(directory):
        return None
    edited_paths = tuple(sorted({path for path in edited if isinstance(path, str) and path}))
    key = (os.path.normcase(directory), edited_paths)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_SECONDS:
            return cached[1]

    result = _read_status(directory, edited_paths)
    with _cache_lock:
        for stale in [entry for entry, (at, _) in _cache.items() if now - at >= _CACHE_SECONDS]:
            del _cache[stale]
        _cache[key] = (now, result)

    return result


def git_diff(directory: str, path: str) -> dict[str, Any] | None:
    """Return the uncommitted changes of one file of the repository holding *directory*.

    Parameters
    ----------
    directory : str
        A directory this host can open (a WSL path already translated).
    path : str
        The file as ``git_status`` lists it: relative to the repository's top
        level, with forward slashes.

    Returns
    -------
    dict or None
        ``{'path', 'diff', 'truncated'}`` - the unified diff against ``HEAD``,
        or for an untracked file its whole content as added lines - or None
        when the directory or path is unusable, the path leaves the
        repository, or git fails.
    """
    if not isinstance(directory, str) or not directory or not os.path.isdir(directory) or not _valid_relative_path(path):
        return None
    toplevel = _git(directory, 'rev-parse', '--show-toplevel')
    if not toplevel or not toplevel.strip():
        return None
    root = toplevel.strip()
    if not _inside(os.path.normcase(os.path.normpath(os.path.join(root, path))), os.path.normcase(os.path.normpath(root))):
        return None

    # Run from the top level, which the listed path is relative to; a literal
    # pathspec, so a name holding * or : is that name and nothing else.
    diff = _git(root, '--literal-pathspecs', 'diff', *_DIFF_OPTIONS, 'HEAD', '--', path)
    if not diff and _git(root, '--literal-pathspecs', 'ls-files', '-z', '--', path) == '':
        # An untracked file: its whole content is new, so compare it with
        # nothing. This form reports a difference with exit code 1.
        diff = _git(root, 'diff', '--no-index', *_DIFF_OPTIONS, '--', '/dev/null', path, ok_codes=(0, 1))
    if diff is None:
        return None

    text, truncated = _clip_diff(diff)
    return {'path': path, 'diff': text, 'truncated': truncated}


def _valid_relative_path(path: object) -> bool:
    """Whether *path* is a relative path below its top level: no drive, no root, no ``..`` part, no NUL."""
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH_CHARS or '\0' in path:
        return False
    if os.path.isabs(path) or path.startswith(('/', '\\')) or os.path.splitdrive(path)[0]:
        return False
    parts = path.replace('\\', '/').split('/')

    return '..' not in parts and all(parts)


def _clip_diff(diff: str) -> tuple[str, bool]:
    """Cut *diff* at the last line end within ``_MAX_DIFF_BYTES``; say whether anything was cut."""
    encoded = diff.encode('utf-8')
    if len(encoded) <= _MAX_DIFF_BYTES:
        return diff, False
    head = encoded[:_MAX_DIFF_BYTES]
    end = head.rfind(b'\n')

    return head[:end + 1 if end >= 0 else len(head)].decode('utf-8', errors='ignore'), True


def _read_status(directory: str, edited: tuple[str, ...]) -> dict[str, Any] | None:
    """Run the three git calls and fold their output into the status record."""
    toplevel = _git(directory, 'rev-parse', '--show-toplevel')
    if not toplevel or not toplevel.strip():
        return None
    status = _git(directory, 'status', '--porcelain=v1', '--branch', '-z', '--untracked-files=normal')
    if status is None:
        return None
    # A repository without a commit has no HEAD to diff against; its files are all untracked or staged.
    numstat = _git(directory, 'diff', '--numstat', '-z', 'HEAD') or ''

    branch, upstream, ahead, behind, entries = _parse_status(status)
    lines = _parse_numstat(numstat)
    root = os.path.normcase(os.path.normpath(toplevel.strip()))

    files: list[dict[str, Any]] = []
    uncommitted: set[str] = set()
    changed = untracked = 0
    for code, path in entries:
        if code == '??':
            untracked += 1
        else:
            changed += 1
        uncommitted.add(os.path.normcase(os.path.normpath(os.path.join(root, path))))
        if len(files) < _MAX_FILES:
            added, removed = lines.get(path, (None, None))
            files.append({'path': path, 'status': code.strip() or code, 'added': added, 'removed': removed})

    edited_in_repo = [path for path in (os.path.normcase(os.path.normpath(item)) for item in edited) if _inside(path, root)]
    edited_set = set(edited_in_repo)
    for record in files:
        record['edited'] = os.path.normcase(os.path.normpath(os.path.join(root, record['path']))) in edited_set

    return {
        'branch': branch, 'upstream': upstream, 'ahead': ahead, 'behind': behind,
        'changed': changed, 'untracked': untracked,
        'added': sum(pair[0] or 0 for pair in lines.values()),
        'removed': sum(pair[1] or 0 for pair in lines.values()),
        'files': files,
        'edited_total': len(edited_set),
        'edited_uncommitted': len(edited_set & uncommitted),
    }


def _parse_status(output: str) -> tuple[str, str, int, int, list[tuple[str, str]]]:
    """Split ``status --porcelain=v1 --branch -z`` output into the branch line's parts and ``(code, path)`` entries."""
    branch = upstream = ''
    ahead = behind = 0
    entries: list[tuple[str, str]] = []
    records = output.split('\0')
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        if record.startswith('## '):
            branch, upstream, ahead, behind = _parse_branch(record[3:])
            continue
        code, path = record[:2], record[3:]
        # A rename or copy is followed by the path it came from, which is not a file of its own.
        if code[0] in 'RC':
            index += 1
        entries.append((code, path))

    return branch, upstream, ahead, behind, entries


def _parse_branch(text: str) -> tuple[str, str, int, int]:
    """Read ``main...origin/main [ahead 2, behind 1]`` into its four parts."""
    head, _, tracking = text.partition(' [')
    branch, _, upstream = head.partition('...')
    if branch.startswith('No commits yet on '):
        branch = branch[len('No commits yet on '):]
    ahead = behind = 0
    for part in tracking.rstrip(']').split(', '):
        word, _, number = part.partition(' ')
        if word in ('ahead', 'behind') and number.isdigit():
            ahead, behind = (int(number), behind) if word == 'ahead' else (ahead, int(number))

    return branch.strip(), upstream.strip(), ahead, behind


def _parse_numstat(output: str) -> dict[str, tuple[int | None, int | None]]:
    """Map each path of ``diff --numstat -z`` output to the lines it adds and removes; a binary file has neither.

    With ``-z`` git writes every path as it is - without it a path with a
    non-ASCII character comes quoted and escaped, and a rename as
    ``old => new``, and neither matched the paths ``status -z`` lists.  A
    record is ``added<TAB>removed<TAB>path``; for a rename or copy the path is
    empty and the old and the new path follow as records of their own, the
    lines counted under the new one.
    """
    lines: dict[str, tuple[int | None, int | None]] = {}
    records = output.split('\0')
    index = 0
    while index < len(records):
        parts = records[index].split('\t')
        index += 1
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path:
            path = records[index + 1] if index + 1 < len(records) else ''
            index += 2
        if path:
            lines[path] = (int(added) if added.isdigit() else None, int(removed) if removed.isdigit() else None)

    return lines


def _inside(path: str, root: str) -> bool:
    """Whether normalized *path* lies under normalized *root*."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _git(directory: str, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str | None:
    """Run one git command in *directory* and return its standard output, or None when it fails.

    *ok_codes* are the exit codes that count as success.
    """
    executable = _git_executable()
    if executable is None:
        return None
    command = [executable, '-c', 'safe.directory=*', '-c', 'core.quotepath=off', '-C', directory, *args]
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=_TIMEOUT_SECONDS, env=environment, stdin=subprocess.DEVNULL,
            **platforms.no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in ok_codes:
        return None

    return result.stdout.decode('utf-8', errors='replace')


def _git_executable() -> str | None:
    """The git executable on ``PATH``, never one in the current directory; None when there is none."""
    name = 'git.exe' if platforms.IS_WINDOWS else 'git'
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if not entry or entry == '.' or not os.path.isabs(entry):
            continue
        candidate = Path(entry) / name
        if candidate.is_file():
            return str(candidate)

    return None
