"""
Last-Seen-Alive Memory
======================

Remembers, in memory only, when each session's process was last observed alive,
so an ended session can be retained for a window measured from *when it ended*
rather than from its last transcript entry.

Without this the retention window (``settings.ENDED_MAX_AGE``) is keyed on
transcript activity age, which answers a different question: a session that sat
idle for hours and is then closed has an activity age far past the window and
drops out of the overview in the very same poll - exactly the session a user
who closed a window by accident is looking for.  Activity age can only ever be
*older* than the moment the process ended (the last entry was written while it
still ran), so measuring from the last sighting is strictly the more accurate
rule, and the activity age remains the fallback for a session this process
never saw alive (one already dead when the monitor started).

Nothing is written to disk: this is a process-local dict, so a restart simply
falls back to the activity-age rule.  It holds only ``(origin, session_id)``
keys and a monotonic timestamp - no path, no content.  ``time.monotonic`` is
used deliberately, so a system clock change cannot make an ended session look
younger or older than it is.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable

__all__ = ['note_alive', 'seconds_since_alive', 'prune_last_seen']

# (origin, session_id) -> time.monotonic() of the last poll that saw it alive.
_last_alive: dict[tuple[str, str], float] = {}

# The snapshot poll and the on-demand history scan run on different pywebview
# worker threads and both record sightings, so guard the dict.
_lock = threading.Lock()


def note_alive(origin: str, session_id: str) -> None:
    """Record that *session_id* on *origin* was just observed alive.

    Parameters
    ----------
    origin : str
        The session root's origin (``windows`` or a WSL distro name); a session
        id is a UUID and thus unique on its own, but pairing it with the origin
        keeps the key consistent with every other cross-root lookup.
    session_id : str
        The session's id.
    """
    if not origin or not session_id:
        return

    with _lock:
        _last_alive[(origin, session_id)] = time.monotonic()


def seconds_since_alive(origin: str, session_id: str) -> float | None:
    """Return how long ago the session was last seen alive, or None if never seen.

    None means this process has no sighting on record - the session was already
    dead when the monitor started, so the caller must fall back to the
    transcript activity age.
    """
    if not origin or not session_id:
        return None

    with _lock:
        seen_at = _last_alive.get((origin, session_id))

    if seen_at is None:
        return None

    return max(0.0, time.monotonic() - seen_at)


def prune_last_seen(active: Iterable[tuple[str, str]]) -> None:
    """Drop sightings for sessions no longer in the registry.

    A session whose registry record is gone can never be shown again (the
    overview is registry-driven), so its sighting is dead weight; dropping it
    keeps the dict bounded over a long-running monitor, mirroring
    ``transcript.prune_scan_cache``.

    Parameters
    ----------
    active : iterable of (origin, session_id)
        Every current registry session across every session root.
    """
    keep = set(active)

    with _lock:
        for key in [key for key in _last_alive if key not in keep]:
            del _last_alive[key]
