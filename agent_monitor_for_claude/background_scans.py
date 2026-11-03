"""
Background Scans
================

Runs the first full scans of large transcripts on one background thread, so a
snapshot never waits on them.  This module only schedules: it knows nothing of
the transcript format.  :mod:`~agent_monitor_for_claude.transcript` hands it a
cache key, a path and the scan to run, and keeps every field that scan reads to
itself - so the privacy boundary stays where it is.

Three things live here: the queue of pending scans, worked through smallest
file first so as many sessions as possible are complete as early as possible;
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

__all__ = ['completed_background_scans', 'drop_pending', 'pause_background_scans', 'queue_scan', 'wait_for_readers']

# A queued scan: called with its cache key and path, it returns True when it
# stored a result.
ScanFn = Callable[[str, Path], bool]


@dataclass
class _DeferredScans:
    """The scans handed to the background thread.

    ``pending`` maps a cache key to the file's size when it was queued, its path
    and the scan to run; the size orders the queue.  ``completed`` counts the
    scans that stored a result.  ``readers`` counts the callers inside
    :func:`pause_background_scans`; ``lock`` is a condition so the worker can
    wait for that count to drop to zero.
    """

    pending: dict[str, tuple[int, Path, ScanFn]] = field(default_factory=dict)
    lock: threading.Condition = field(default_factory=threading.Condition)
    worker: threading.Thread | None = None
    completed: int = 0
    readers: int = 0


_deferred = _DeferredScans()


def queue_scan(cache_key: str, path: Path, size: int, scan: ScanFn) -> None:
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
        The file's size now; smaller files are scanned first.
    scan : callable
        Called on the background thread with ``(cache_key, path)``; returns
        True if it stored a result.
    """
    with _deferred.lock:
        _deferred.pending.setdefault(cache_key, (size, path, scan))
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
    """Work through the queued scans, smallest file first, until none is left."""
    while True:
        with _deferred.lock:
            if not _deferred.pending:
                _deferred.worker = None
                return
            cache_key, (_, path, scan) = min(_deferred.pending.items(), key=lambda item: item[1][0])

        try:
            stored = scan(cache_key, path)
        except Exception:
            # Last-resort isolation, like build_snapshot's per record: one odd
            # file must not stop every scan queued behind it.  Nothing is stored,
            # so the next snapshot queues it again.
            stored = False

        with _deferred.lock:
            _deferred.pending.pop(cache_key, None)
            if stored:
                _deferred.completed += 1
