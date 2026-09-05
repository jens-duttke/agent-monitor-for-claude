"""
Paths
=====

Resolves the Claude config directory and derives the session-registry and
transcript locations from it.  This is the one module that knows both the
Claude Code on-disk layout and where each session root lives, so a layout
change - or a new kind of root - is contained here.

Every layout function below takes a :class:`SessionRoot` as its first
parameter: a session's registry, transcripts, background-task output, and
scratchpad all live under that root's ``config_dir``/``claude_temp_dir``
rather than a single implicit location.  :func:`local_root` builds the root for
the Claude Code install on the machine this monitor runs on - Windows or Linux;
a WSL distro's root is built in ``wsl.py`` but consumed identically by every
function here.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .platforms import IS_WINDOWS

__all__ = [
    'SessionRoot', 'local_root', 'claude_temp_dir', 'config_dir', 'sessions_dir', 'projects_dir', 'cwd_to_slug',
    'transcript_path', 'task_output_dir', 'task_output_path', 'scratchpad_dir', 'host_path',
]

_NON_ALNUM_PATTERN = re.compile(r'[^A-Za-z0-9]')

# Directory Claude Code keeps its per-session temp tree in, below the system
# temp directory.  Windows' temp directory is already per-user, so the name is
# plain; a POSIX ``/tmp`` is shared, so the owner's uid is appended there.
_TEMP_DIR_NAME = 'claude'
_FOREIGN_TEMP_DIR_PATTERN = 'claude-*'

# Windows drive letters WSL maps under /mnt, e.g. /mnt/c -> C:\.
_WSL_MOUNT_PATTERN = re.compile(r'^/mnt/([A-Za-z])(/.*)?$')


@dataclass(frozen=True)
class SessionRoot:
    """One place Claude Code sessions can live: the local install, or one WSL distro.

    Every path-layout function in this module takes a ``SessionRoot`` as its first parameter and
    derives its result from ``config_dir``/``claude_temp_dir`` alone, so the rest of the application never
    hardcodes a location.

    Attributes
    ----------
    origin : str
        Stable identifier for this root: ``'local'`` or ``'wsl:<distro>'``.
    label : str or None
        Display name for the UI - the distro name for a WSL root, ``None`` for the local one.
    config_dir : Path
        The root's ``.claude`` directory (holds ``sessions/`` and ``projects/``).
    proc_dir : Path or None
        A *foreign* ``/proc`` directory to read for liveness and descendant probing - a WSL distro's,
        shared over ``\\\\wsl.localhost``.  ``None`` for the local root, which is probed through the
        running system's own process API (``process_probe``) instead, whichever system that is.
    claude_temp_dir : Path
        The directory Claude Code writes each session's background-task output and scratchpad into:
        ``<temp>/claude`` on Windows, ``<temp>/claude-<uid>`` on Linux, where ``/tmp`` is shared
        between users.
    """

    origin: str
    label: str | None
    config_dir: Path
    proc_dir: Path | None
    claude_temp_dir: Path


def local_root() -> SessionRoot:
    """Return the session root for the Claude Code install on this machine."""
    return SessionRoot('local', None, config_dir(), None, claude_temp_dir(Path(tempfile.gettempdir())))


def claude_temp_dir(temp_dir: Path) -> Path:
    """Return the Claude Code temp directory below *temp_dir*.

    Claude Code writes its per-session temp tree (background-task output, scratchpad) into a
    directory of its own below the system temp directory: ``claude`` on Windows, where the temp
    directory already belongs to one user, and ``claude-<uid>`` on POSIX, where ``/tmp`` is shared.

    For *this* machine's own temp directory the answer is computed, never looked up - the running
    system says which of the two names applies, and the uid is our own.  That matters because the
    local root is rebuilt on every poll: a directory listing here would be a per-second scan of a
    temp folder that can hold thousands of entries.

    A *foreign* tree - a WSL distro read from a Windows host - has no uid to ask for, so it is
    resolved by looking: the single existing ``claude-*`` directory is taken when there is exactly
    one, which is the case for a distro with one user running Claude Code.  With none, or with
    several and no way to tell them apart, the plain ``claude`` name is returned - a path that may
    not exist, which every caller already handles.  That lookup only happens during WSL discovery,
    which is itself cached.

    Parameters
    ----------
    temp_dir : Path
        The root's system temp directory (``%TEMP%``, or a distro's ``/tmp``).
    """
    if temp_dir == Path(tempfile.gettempdir()):
        return temp_dir / (_TEMP_DIR_NAME if IS_WINDOWS else f'{_TEMP_DIR_NAME}-{os.getuid()}')

    try:
        candidates = sorted(entry for entry in temp_dir.glob(_FOREIGN_TEMP_DIR_PATTERN) if entry.is_dir())
    except OSError:
        candidates = []

    if len(candidates) == 1:
        return candidates[0]

    return temp_dir / _TEMP_DIR_NAME


def config_dir() -> Path:
    """Return the Claude config directory.

    Honors ``CLAUDE_CONFIG_DIR`` if set, otherwise defaults to ``~/.claude``.
    """
    custom = os.environ.get('CLAUDE_CONFIG_DIR')
    if custom:
        return Path(custom)

    return Path.home() / '.claude'


def sessions_dir(root: SessionRoot) -> Path:
    """Return the directory holding the per-process session registry files."""
    return root.config_dir / 'sessions'


def projects_dir(root: SessionRoot) -> Path:
    """Return the directory holding the per-project transcript folders."""
    return root.config_dir / 'projects'


def cwd_to_slug(cwd: str) -> str:
    """Convert a working directory to its Claude Code project-folder slug.

    Claude Code replaces every character that is not a letter or digit - the
    drive colon, path separators, dots, and any other punctuation - with a
    single hyphen, one hyphen per character (consecutive separators are never
    collapsed).  For example ``d:\\WebDev\\HexEd.it`` becomes
    ``d--WebDev-HexEd-it`` and ``d:\\WebDev\\oku3d-app`` becomes
    ``d--WebDev-oku3d-app``.  The same scheme applies to a WSL POSIX cwd (e.g.
    ``/home/dev/proj`` becomes ``-home-dev-proj``).

    Parameters
    ----------
    cwd : str
        Absolute working directory as reported by the session registry.
    """
    return _NON_ALNUM_PATTERN.sub('-', cwd)


def transcript_path(root: SessionRoot, session_id: str, cwd: str) -> Path:
    """Return the expected transcript path for a session under *root*.

    The file may not exist (a freshly opened session has no transcript yet);
    callers must check.
    """
    return projects_dir(root) / cwd_to_slug(cwd) / f'{session_id}.jsonl'


def task_output_dir(root: SessionRoot, session_id: str, cwd: str) -> Path:
    """Return the directory holding a session's background-task output files under *root*.

    Claude Code writes the live stdout/stderr of each ``run_in_background`` task
    to ``<claude-temp>/<project-slug>/<session-id>/tasks/<task-id>.output`` and
    tells the model to ``Read`` that file for interim output.  The directory may
    not exist (a session that never ran a background task); callers must check.
    """
    return root.claude_temp_dir / cwd_to_slug(cwd) / session_id / 'tasks'


def task_output_path(root: SessionRoot, session_id: str, cwd: str, task_id: str) -> Path:
    """Return the output-file path for one background task under *root*.

    The returned path is not validated here - the caller confines it to
    ``task_output_dir`` (``relative_to``) and validates the ids before reading.
    """
    return task_output_dir(root, session_id, cwd) / f'{task_id}.output'


def scratchpad_dir(root: SessionRoot, session_id: str, cwd: str) -> Path:
    """Return the session's scratchpad directory under *root* (sibling of the tasks directory).

    Claude Code hands the session a scratchpad under
    ``<claude-temp>/<project-slug>/<session-id>/scratchpad`` for temporary files;
    a background task often redirects its output into a file there.
    """
    return root.claude_temp_dir / cwd_to_slug(cwd) / session_id / 'scratchpad'


def host_path(root: SessionRoot, path_text: str) -> str:
    """Translate a path a session reported into a form this host can open.

    A WSL root only ever exists on a Windows host (see ``wsl.py``), and the two rewrites below
    exist solely to reach one from there:

    * a ``/mnt/<drive>/...`` path - WSL's view of a Windows drive - becomes its ``<DRIVE>:\\...``
      form, reachable directly without going through any distro;
    * any other absolute POSIX path is a path inside the distro's own filesystem and becomes the
      UNC form ``\\\\wsl.localhost\\<label>\\...``.

    On a Linux host neither applies: every path a session reports is already a path this machine
    can open, and ``/mnt/c/...`` is an ordinary directory rather than a drive to rewrite - so the
    text is returned unchanged.  Anything else (already a Windows path, a relative path, ...)
    passes through unchanged on either host.

    Parameters
    ----------
    root : SessionRoot
        The session's root; only ``root.label`` is consulted.
    path_text : str
        The path as reported by the session (a Bash redirect target, for example).
    """
    if not IS_WINDOWS:
        return path_text

    match = _WSL_MOUNT_PATTERN.match(path_text)
    if match:
        drive = match.group(1).upper()
        rest = (match.group(2) or '').replace('/', '\\')
        return f'{drive}:{rest}' if rest else f'{drive}:\\'

    if root.label and path_text.startswith('/'):
        return '\\\\wsl.localhost\\' + root.label + path_text.replace('/', '\\')

    return path_text
