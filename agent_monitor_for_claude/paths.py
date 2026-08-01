"""
Paths
=====

Resolves the Claude config directory and derives the session-registry and
transcript locations from it.  This is the one module that knows both the
Claude Code on-disk layout and where each session root lives, so a layout
change - or a new kind of root - is contained here.

Every layout function below takes a :class:`SessionRoot` as its first
parameter: a session's registry, transcripts, background-task output, and
scratchpad all live under that root's ``config_dir``/``temp_dir`` rather than
a single implicit location.  :func:`windows_root` builds the root for the
native Windows install; a WSL distro's root is built elsewhere (a later
addition) but consumed identically by every function here.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    'SessionRoot', 'windows_root', 'config_dir', 'sessions_dir', 'projects_dir', 'cwd_to_slug', 'transcript_path',
    'task_output_dir', 'task_output_path', 'scratchpad_dir', 'wsl_path_to_windows',
]

_NON_ALNUM_PATTERN = re.compile(r'[^A-Za-z0-9]')

# Windows drive letters WSL maps under /mnt, e.g. /mnt/c -> C:\.
_WSL_MOUNT_PATTERN = re.compile(r'^/mnt/([A-Za-z])(/.*)?$')


@dataclass(frozen=True)
class SessionRoot:
    """One place Claude Code sessions can live: the native Windows install, or one WSL distro.

    Every path-layout function in this module takes a ``SessionRoot`` as its first parameter and
    derives its result from ``config_dir``/``temp_dir`` alone, so the rest of the application never
    hardcodes a location.

    Attributes
    ----------
    origin : str
        Stable identifier for this root: ``'windows'`` or ``'wsl:<distro>'``.
    label : str or None
        Display name for the UI - the distro name for a WSL root, ``None`` for Windows.
    config_dir : Path
        The root's ``.claude`` directory (holds ``sessions/`` and ``projects/``).
    proc_dir : Path or None
        The root's ``/proc`` directory (WSL only, for liveness and descendant probing); ``None`` for
        Windows, which is probed through the native process APIs instead.
    temp_dir : Path
        The root's temp directory, under which Claude Code writes background-task output and each
        session's scratchpad.
    """

    origin: str
    label: str | None
    config_dir: Path
    proc_dir: Path | None
    temp_dir: Path


def windows_root() -> SessionRoot:
    """Return the session root for the native Windows Claude Code install."""
    return SessionRoot('windows', None, config_dir(), None, Path(tempfile.gettempdir()))


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
    to ``<temp>/claude/<project-slug>/<session-id>/tasks/<task-id>.output`` and
    tells the model to ``Read`` that file for interim output.  The directory may
    not exist (a session that never ran a background task); callers must check.
    """
    return root.temp_dir / 'claude' / cwd_to_slug(cwd) / session_id / 'tasks'


def task_output_path(root: SessionRoot, session_id: str, cwd: str, task_id: str) -> Path:
    """Return the output-file path for one background task under *root*.

    The returned path is not validated here - the caller confines it to
    ``task_output_dir`` (``relative_to``) and validates the ids before reading.
    """
    return task_output_dir(root, session_id, cwd) / f'{task_id}.output'


def scratchpad_dir(root: SessionRoot, session_id: str, cwd: str) -> Path:
    """Return the session's scratchpad directory under *root* (sibling of the tasks directory).

    Claude Code hands the session a scratchpad under
    ``<temp>/claude/<project-slug>/<session-id>/scratchpad`` for temporary files;
    a background task often redirects its output into a file there.
    """
    return root.temp_dir / 'claude' / cwd_to_slug(cwd) / session_id / 'scratchpad'


def wsl_path_to_windows(root: SessionRoot, path_text: str) -> str:
    """Translate a path a WSL session reported into a Windows-readable form.

    A ``/mnt/<drive>/...`` path (WSL's view of a Windows drive) always translates to its
    ``<DRIVE>:\\...`` form, regardless of *root* - the Windows drive is reachable directly, without
    going through any distro.

    Any other absolute POSIX path (starting with ``/``) is a path inside the distro's own
    filesystem: on a *root* that carries a ``label`` (a WSL root), it translates to the UNC form
    ``\\\\wsl.localhost\\<label>\\...``; on the Windows root (``label`` is ``None``) there is no
    distro to route it through, so it is left unchanged, matching today's behavior.

    Anything else (already a Windows path, a relative path, ...) passes through unchanged.

    Parameters
    ----------
    root : SessionRoot
        The session's root; only ``root.label`` is consulted.
    path_text : str
        The path as reported by the session (a Bash redirect target, for example).
    """
    match = _WSL_MOUNT_PATTERN.match(path_text)
    if match:
        drive = match.group(1).upper()
        rest = (match.group(2) or '').replace('/', '\\')
        return f'{drive}:{rest}' if rest else f'{drive}:\\'

    if root.label and path_text.startswith('/'):
        return '\\\\wsl.localhost\\' + root.label + path_text.replace('/', '\\')

    return path_text
