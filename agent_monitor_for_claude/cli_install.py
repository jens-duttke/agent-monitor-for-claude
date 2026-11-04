"""
CLI Install
===========

The Claude Code version installed under a session root, from the outcome of its last self-update.

When Claude Code updates its native install it records the result in ``.last-update-result.json`` in
its config directory - where it came from, the version it moved to, and whether that worked.  A
session started before the update keeps running the old version until it is restarted, and the
installed version is what tells the UI so.  Only the outcome and the target version are taken; a
failed update, a missing or reshaped file yield no installed version, and the UI then compares the
live sessions with each other alone.

The file changes only when Claude Code updates itself, so each root's answer is re-read at most once
per ``_REFRESH_SECONDS``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .paths import SessionRoot, update_result_path

__all__ = ['installed_version', 'parse_version']

_REFRESH_SECONDS = 30.0

# Dot-separated numbers, as Claude Code numbers its releases; the length cap keeps an
# absurd value from reaching the UI at all.
_VERSION_PATTERN = re.compile(r'\d{1,6}(?:\.\d{1,6}){1,3}')

# The outcome (written as both ``outcome`` and ``status``) that makes the target version the installed one.
_SUCCESS = 'success'

# Update-result path -> (monotonic time read, installed version or None).
_versions: dict[str, tuple[float, str | None]] = {}
_versions_lock = threading.Lock()


def installed_version(root: SessionRoot) -> str | None:
    """Return the Claude Code version the last successful self-update installed under *root*, or None.

    Parameters
    ----------
    root : SessionRoot
        The session root whose config directory holds the update result - a WSL distro keeps its own.
    """
    path = update_result_path(root)
    key = str(path)
    now = time.monotonic()
    with _versions_lock:
        cached = _versions.get(key)
        if cached is not None and now - cached[0] < _REFRESH_SECONDS:
            return cached[1]

    try:
        with open(path, encoding='utf-8') as handle:
            version = _version_from(json.load(handle))
    except (OSError, ValueError):
        version = None

    with _versions_lock:
        _versions[key] = (now, version)

    return version


def parse_version(value: Any) -> str | None:
    """Return *value* if it is a release version such as ``2.1.318``, else None."""
    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        return None

    return value


def _version_from(data: Any) -> str | None:
    """Return the version a parsed update result installed, or None when it records no successful update."""
    if not isinstance(data, dict):
        return None
    if data.get('outcome') != _SUCCESS and data.get('status') != _SUCCESS:
        return None

    return parse_version(data.get('version_to'))
