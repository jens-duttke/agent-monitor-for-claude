"""
Background Scans
================

Runs the first full scans of large transcripts on one background thread, so a
snapshot never waits on them.  This module only schedules: it knows nothing of
the transcript format.  :mod:`~agent_monitor_for_claude.transcript` hands it a
cache key, a path and the scan to run, and keeps every field that scan reads to
itself - so the privacy boundary stays where it is.

Three things live here: the queue of pending scans, worked through the
session you selected first and then the most recently active file, so what you
are looking at is complete first and the list fills in from the top;
the count of scans that stored a result, which is what tells a change
fingerprint that a snapshot now has more to show; and the pause a caller holds
while it builds a snapshot (see :func:`pause_background_scans`).

Side effects are limited to one daemon thread, started when the first scan is
queued and ended when the queue runs empty.  Nothing is read here; the scan
callable does that.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Container, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    'completed_background_scans', 'drop_pending', 'pause_background_scans', 'prioritize', 'queue_scan', 'wait_for_readers',
]

# A queued scan: called with its cache key and path, it returns True when it
# stored a result.
ScanFn = Callable[[str, Path], bool]


@dataclass
class _DeferredScans:
    """The scans handed to the background thread.

    ``pending`` maps a cache key to the file's size and modification time when
    it was queued, its path and the scan to run; ``urgent`` holds the keys asked
    for ahead of the rest (see :func:`prioritize`).  ``completed`` counts the
    scans that stored a result.  ``readers`` counts the callers inside
    :func:`pause_background_scans`; ``lock`` is a condition so the worker can
    wait for that count to drop to zero.
    """

    pending: dict[str, tuple[int, float, Path, ScanFn]] = field(default_factory=dict)
    urgent: set[str] = field(default_factory=set)
    lock: threading.Condition = field(default_factory=threading.Condition)
    worker: threading.Thread | None = None
    completed: int = 0
    readers: int = 0


_deferred = _DeferredScans()


def queue_scan(cache_key: str, path: Path, size: int, scan: ScanFn, modified: float = 0.0) -> None:
    """Queue *scan* of *path*, starting the background thread if none is running.

    A key already queued keeps its first entry, so a snapshot asking again while
    the scan waits does not move it in the queue.

    Parameters
    ----------
    cache_key : str
        The key the scan stores its result under, which is also the queue key.
    path : Path
        The file to scan.
    size : int
        The file's size now; among files written at the same moment the smaller
        is scanned first.
    scan : callable
        Called on the background thread with ``(cache_key, path)``; returns
        True if it stored a result.
    modified : float
        The file's modification time (POSIX seconds); the most recently written
        file is scanned first, since a session that just did something is the
        one most likely to be looked at.
    """
    with _deferred.lock:
        _deferred.pending.setdefault(cache_key, (size, modified, path, scan))
        if _deferred.worker is None:
            _deferred.worker = threading.Thread(target=_run, name='transcript-scan', daemon=True)
            _deferred.worker.start()


def drop_pending(keep: Container[str]) -> None:
    """Drop every queued scan whose cache key is not in *keep*.

    A queued scan of a session that has left the registry would only produce a
    cache entry the caller is about to evict.
    """
    with _deferred.lock:
        for key in list(_deferred.pending):
            if key not in keep:
                del _deferred.pending[key]
                _deferred.urgent.discard(key)


def prioritize(matches: Callable[[str], bool]) -> int:
    """Move the queued scans whose cache key *matches* to the front of the queue.

    The selected session's scans are the ones someone is waiting to see, so they
    jump the queue - the transcript and every subagent file of that session.
    Returns how many queued scans were moved.
    """
    with _deferred.lock:
        keys = [key for key in _deferred.pending if matches(key)]
        _deferred.urgent.update(keys)
        return len(keys)


def completed_background_scans() -> int:
    """Return how many background scans have stored their result so far."""
    with _deferred.lock:
        return _deferred.completed


@contextmanager
def pause_background_scans() -> Iterator[None]:
    """Hold the background scans at their next chunk boundary while the caller runs.

    The scan is CPU-bound and holds the interpreter lock in long stretches, so
    a snapshot sharing the interpreter with it waits for the lock after every
    file access it makes: measured, a snapshot took ~1 s instead of ~0.1 s for
    as long as a scan ran, the first one ~1.4 s instead of ~0.5 s.  Holding the
    scan for those few milliseconds costs it nothing it does not make up.  The
    scan gives way only where it calls :func:`wait_for_readers`.
    """
    with _deferred.lock:
        _deferred.readers += 1
    try:
        yield
    finally:
        with _deferred.lock:
            _deferred.readers -= 1
            _deferred.lock.notify_all()


def wait_for_readers() -> None:
    """Block the calling scan while any caller is inside :func:`pause_background_scans`."""
    with _deferred.lock:
        _deferred.lock.wait_for(lambda: _deferred.readers == 0)


def _run() -> None:
    """Work through the queued scans in the order of :func:`_next_order` until none is left."""
    while True:
        with _deferred.lock:
            if not _deferred.pending:
                _deferred.worker = None
                return
            cache_key, (_, _, path, scan) = min(_deferred.pending.items(), key=_next_order)

        try:
            stored = scan(cache_key, path)
        except Exception:
            # Last-resort isolation, like build_snapshot's per record: one odd
            # file must not stop every scan queued behind it.  Nothing is stored,
            # so the next snapshot queues it again.
            stored = False

        with _deferred.lock:
            _deferred.pending.pop(cache_key, None)
            _deferred.urgent.discard(cache_key)
            if stored:
                _deferred.completed += 1


def _next_order(item: tuple[str, tuple[int, float, Path, ScanFn]]) -> tuple[bool, float, int]:
    """Sort key of a queued scan: a prioritized one first, then the newest file, then the smallest."""
    cache_key, (size, modified, _, _) = item
    return (cache_key not in _deferred.urgent, -modified, size)
