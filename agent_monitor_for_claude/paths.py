"""
Paths
=====

Resolves the Claude config directory and derives the session-registry and
transcript locations from it.  Encapsulates the one place that knows how
Claude Code lays out its on-disk files, so a layout change is contained here.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

__all__ = [
    'config_dir', 'sessions_dir', 'projects_dir', 'cwd_to_slug', 'transcript_path',
    'task_output_dir', 'task_output_path', 'scratchpad_dir',
]

_NON_ALNUM_PATTERN = re.compile(r'[^A-Za-z0-9]')


def config_dir() -> Path:
    """Return the Claude config directory.

    Honors ``CLAUDE_CONFIG_DIR`` if set, otherwise defaults to ``~/.claude``.
    """
    custom = os.environ.get('CLAUDE_CONFIG_DIR')
    if custom:
        return Path(custom)

    return Path.home() / '.claude'


def sessions_dir() -> Path:
    """Return the directory holding the per-process session registry files."""
    return config_dir() / 'sessions'


def projects_dir() -> Path:
    """Return the directory holding the per-project transcript folders."""
    return config_dir() / 'projects'


def cwd_to_slug(cwd: str) -> str:
    """Convert a working directory to its Claude Code project-folder slug.

    Claude Code replaces every character that is not a letter or digit - the
    drive colon, path separators, dots, and any other punctuation - with a
    single hyphen, one hyphen per character (consecutive separators are never
    collapsed).  For example ``d:\\WebDev\\HexEd.it`` becomes
    ``d--WebDev-HexEd-it`` and ``d:\\WebDev\\oku3d-app`` becomes
    ``d--WebDev-oku3d-app``.

    Parameters
    ----------
    cwd : str
        Absolute working directory as reported by the session registry.
    """
    return _NON_ALNUM_PATTERN.sub('-', cwd)


def transcript_path(session_id: str, cwd: str) -> Path:
    """Return the expected transcript path for a session.

    The file may not exist (a freshly opened session has no transcript yet);
    callers must check.
    """
    return projects_dir() / cwd_to_slug(cwd) / f'{session_id}.jsonl'


def task_output_dir(session_id: str, cwd: str) -> Path:
    """Return the directory holding a session's background-task output files.

    Claude Code writes the live stdout/stderr of each ``run_in_background`` task
    to ``<temp>/claude/<project-slug>/<session-id>/tasks/<task-id>.output`` and
    tells the model to ``Read`` that file for interim output.  The directory may
    not exist (a session that never ran a background task); callers must check.
    """
    return Path(tempfile.gettempdir()) / 'claude' / cwd_to_slug(cwd) / session_id / 'tasks'


def task_output_path(session_id: str, cwd: str, task_id: str) -> Path:
    """Return the output-file path for one background task.

    The returned path is not validated here - the caller confines it to
    ``task_output_dir`` (``relative_to``) and validates the ids before reading.
    """
    return task_output_dir(session_id, cwd) / f'{task_id}.output'


def scratchpad_dir(session_id: str, cwd: str) -> Path:
    """Return the session's scratchpad directory (sibling of the tasks directory).

    Claude Code hands the session a scratchpad under
    ``<temp>/claude/<project-slug>/<session-id>/scratchpad`` for temporary files;
    a background task often redirects its output into a file there.
    """
    return Path(tempfile.gettempdir()) / 'claude' / cwd_to_slug(cwd) / session_id / 'scratchpad'
