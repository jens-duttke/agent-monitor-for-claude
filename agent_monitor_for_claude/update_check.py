"""
Update check
============

Whether a newer release of this application exists, asked of GitHub's public
API only while the ``update_check`` setting is on (it is off by default).  It
is the application's one outbound connection: an anonymous ``GET`` of the
repository's latest release, of which only ``tag_name`` is read.  Nothing is
sent but the request itself, nothing is downloaded or installed - the page
only links to the release.

:func:`update_status` never waits for the network: it answers from memory and,
when the last answer is older than :data:`_CHECK_INTERVAL_SECONDS`, starts one
request on a daemon thread, whose answer the next call returns.  A failed
request is retried after :data:`_RETRY_SECONDS`.  Nothing is written to disk.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Any, Callable

from . import __version__
from .settings import current_settings

__all__ = ['LATEST_RELEASE_API', 'update_status']

LATEST_RELEASE_API = 'https://api.github.com/repos/jens-duttke/agent-monitor-for-claude/releases/latest'

_CHECK_INTERVAL_SECONDS = 6 * 3600
_RETRY_SECONDS = 15 * 60
_TIMEOUT_SECONDS = 10
# A release record is a few kilobytes plus its notes; anything near this is not one.
_MAX_RESPONSE_BYTES = 1024 * 1024
_TAG_PATTERN = re.compile(r'^v?(\d{1,6}\.\d{1,6}\.\d{1,6})$')
# A generic client name: GitHub refuses a request without one, and nothing about the user belongs in it.
_HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'agent-monitor-for-claude'}


class _State:
    """The last answer and when the next request is due, guarded by ``lock``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latest: str | None = None
        self.checked_at: float | None = None
        self.failed = False
        self.running = False
        self.next_due = 0.0


_state = _State()


def update_status(fetch: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """Return what is known about a newer release, starting a check when one is due.

    Parameters
    ----------
    fetch
        The request to run, returning the latest version; the GitHub request
        by default.  Tests pass a stand-in.

    Returns
    -------
    dict
        ``enabled`` (the setting), ``current`` (this build's version) and,
        while enabled, ``latest`` (the newest release's version, or ``None``
        before a first answer), ``checked_at`` (POSIX seconds of that answer),
        ``pending`` (a request is under way) and ``failed`` (the last request
        failed).
    """
    if not current_settings().update_check:
        return {'enabled': False, 'current': __version__}

    with _state.lock:
        now = time.monotonic()
        if not _state.running and now >= _state.next_due:
            _state.running = True
            threading.Thread(target=_run_check, args=(fetch or _fetch_latest,), daemon=True, name='update-check').start()

        return {
            'enabled': True, 'current': __version__, 'latest': _state.latest, 'checked_at': _state.checked_at,
            'pending': _state.running, 'failed': _state.failed,
        }


def _run_check(fetch: Callable[[], str | None]) -> None:
    """Run one request and store its answer; any failure keeps the previous answer."""
    try:
        latest = fetch()
    except Exception:  # noqa: BLE001 - offline, a proxy, a changed API: all mean "no answer this time"
        latest = None

    with _state.lock:
        _state.running = False
        if latest is None:
            _state.failed = True
            _state.next_due = time.monotonic() + _RETRY_SECONDS
            return

        _state.latest = latest
        _state.checked_at = time.time()
        _state.failed = False
        _state.next_due = time.monotonic() + _CHECK_INTERVAL_SECONDS


def _fetch_latest() -> str | None:
    """Ask GitHub for the latest release and return its version, or ``None`` when the answer names none."""
    request = urllib.request.Request(LATEST_RELEASE_API, headers=_HEADERS, method='GET')
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)

    if len(body) > _MAX_RESPONSE_BYTES:
        return None

    return _parse_release(body)


def _parse_release(body: bytes) -> str | None:
    """The version of a release record's ``tag_name`` (``v1.2.3`` -> ``1.2.3``), or ``None``."""
    try:
        record = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    tag = record.get('tag_name') if isinstance(record, dict) else None
    match = _TAG_PATTERN.match(tag) if isinstance(tag, str) else None

    return match.group(1) if match else None


def _reset() -> None:
    """Forget every answer (tests)."""
    with _state.lock:
        _state.latest = None
        _state.checked_at = None
        _state.failed = False
        _state.running = False
        _state.next_due = 0.0
