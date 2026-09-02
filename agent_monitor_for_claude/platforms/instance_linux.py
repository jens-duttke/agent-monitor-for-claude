"""
Linux Single-Instance Guard
============================

Prevents multiple windows from running simultaneously using an advisory
``flock`` on a lock file in the session's runtime directory.  The holder's PID
and version are stored in that file so a new instance can identify and replace
it regardless of executable name.

See :mod:`agent_monitor_for_claude.single_instance` for the dispatch and for
what the guard promises.  The lock file is the one file this application writes
on Linux besides the browser profile; it is created with owner-only permissions
and lives in the runtime directory the system clears at logout.

The lock is held by an open file descriptor for the process lifetime, and the
kernel releases it when the process exits - so a crashed instance never leaves
behind a lock a restart cannot take.
"""
from __future__ import annotations

import errno
import fcntl
import os
import signal
import time
from pathlib import Path

from .. import __version__
from .. import procfs
from ..i18n import T
from .linux import ask_yes_no

__all__ = ['ensure_single_instance', 'release_instance_lock']

# English last-resort text for the empty-translations degradation path (when
# every locale candidate, including en.json, failed to load and T is empty).
_DEFAULT_APP_TITLE = 'Agent Monitor for Claude'
_DEFAULT_ALREADY_RUNNING = ('Agent Monitor for Claude v{running_version} is already running.\n\n'
                            'Do you want to replace the running instance?')

_LOCK_FILE_NAME = 'agent-monitor-for-claude.lock'

# Seconds to wait for a terminated holder to actually exit before escalating,
# and how often to look while waiting.
_TERMINATE_TIMEOUT = 5.0
_TERMINATE_POLL = 0.1

# File descriptor kept open for the process lifetime; releasing it drops the
# lock.  Released on exit or explicitly via release_instance_lock().
_lock_fd: int | None = None


def ensure_single_instance() -> bool:
    """Ensure only one instance runs; offer to replace a running one.

    Returns
    -------
    bool
        True if this instance may proceed, False if it should exit.
    """
    global _lock_fd

    path = _lock_path()
    try:
        fd = _acquire(path)
    except OSError:
        # Locking is unavailable here (a filesystem with no lock support, say),
        # not held by someone else.  Fail open - let this instance run rather
        # than refuse to start over a rare API failure - exactly as the Windows
        # guard does when the mutex API fails outright.  No holder record is
        # written, since none is backed by a held lock.
        return True

    if fd is not None:
        _lock_fd = fd
        _store_holder_info(fd)
        return True

    holder_pid, running_version = _read_holder_info()

    # T is empty when every locale candidate failed to load (its documented
    # last-resort). Read through .get with English defaults so that degradation
    # still shows a dialog instead of crashing startup with a KeyError.
    title = T.get('app_title', _DEFAULT_APP_TITLE)
    if running_version:
        title += f' v{running_version}'

    template = T.get('already_running', _DEFAULT_ALREADY_RUNNING)
    try:
        message = template.format(running_version=running_version or '?')
    except (KeyError, IndexError, ValueError):
        # A translator-supplied template with a wrong placeholder must not crash.
        message = _DEFAULT_ALREADY_RUNNING.format(running_version=running_version or '?')

    if not ask_yes_no(message, title):
        return False

    # The dialog can sit open indefinitely; re-read the holder at click time. A
    # holder that exited meanwhile leaves a stale record, and the kernel recycles
    # pids - terminating the pid read before the dialog could kill an unrelated
    # process, so it is only terminated when it is still the recorded holder.
    current_holder_pid, _ = _read_holder_info()
    if holder_pid and current_holder_pid == holder_pid:
        _terminate_pid(holder_pid)

    # Retaking the lock is the ground truth for whether the old instance is
    # really gone: the kernel only frees it when that process exits.  Failing
    # here leaves the old instance running and exits, rather than starting a
    # second window and hijacking the holder record.
    fd = _acquire(path)
    if fd is None:
        return False

    _lock_fd = fd
    _store_holder_info(fd)

    return True


def release_instance_lock() -> None:
    """Release the lock so a new instance can start.

    The file itself is deliberately left behind.  Unlinking it would let a
    second process create a fresh file and lock that while a third still holds a
    lock on the unlinked inode - two instances, each convinced it is the only
    one.  A stale file costs nothing: the lock lives in the open descriptor, so
    the next start takes it without resistance.
    """
    global _lock_fd

    if _lock_fd is None:
        return

    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass

    try:
        os.close(_lock_fd)
    except OSError:
        pass

    _lock_fd = None


def _lock_directory() -> Path:
    """Return the directory the lock file lives in.

    ``XDG_RUNTIME_DIR`` is the correct home for runtime state - it is per-user,
    on tmpfs, and cleared at logout.  Sessions without it (some minimal or
    containerised setups) fall back to the user's cache directory, which is
    still per-user and writable.
    """
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    if runtime_dir:
        return Path(runtime_dir)

    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')


def _lock_path() -> Path:
    """Return the lock file path."""
    return _lock_directory() / _LOCK_FILE_NAME


def _acquire(path: Path) -> int | None:
    """Open *path* and take the exclusive lock, or None if it is held elsewhere.

    The descriptor is returned still open and locked; the caller owns it.  A
    directory that cannot be created or a file that cannot be opened yields
    ``None`` as well - the same outcome as a lock held by someone else, which is
    the safe direction: the guard refuses rather than assuming it is alone.

    Raises
    ------
    OSError
        When ``flock`` itself is unavailable rather than contended - the caller
        tells that apart, because "someone else holds it" and "this filesystem
        cannot lock" call for opposite answers.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise

    return fd


def _store_holder_info(fd: int) -> None:
    """Write this process's PID and version into the locked file."""
    payload = f'{os.getpid()}\n{__version__}\n'.encode('utf-8')
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


def _read_holder_info() -> tuple[int | None, str | None]:
    """Read the PID and version of the lock-holding instance.

    ``flock`` is advisory, so the file stays readable while another process
    holds the lock.  A missing or malformed file yields ``(None, None)``.
    """
    try:
        raw = _lock_path().read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None, None

    lines = raw.splitlines()
    if not lines:
        return None, None

    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None, None

    version = lines[1].strip() if len(lines) > 1 else ''

    return pid or None, version or None


def _terminate_pid(pid: int) -> None:
    """Ask a process to exit and wait until it is gone.

    Sends ``SIGTERM`` so the holder can close its window in an orderly way, then
    escalates to ``SIGKILL`` if it is still alive when the timeout expires.
    Returning does not guarantee the process died - the caller proves that by
    retaking the lock.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    if _wait_for_exit(pid):
        return

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return

    _wait_for_exit(pid)


def _wait_for_exit(pid: int) -> bool:
    """Wait up to ``_TERMINATE_TIMEOUT`` for *pid* to disappear; True if it did."""
    deadline = time.monotonic() + _TERMINATE_TIMEOUT
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(_TERMINATE_POLL)

    return not _process_is_alive(pid)


def _process_is_alive(pid: int) -> bool:
    """Return True if a process with *pid* is still running.

    A signal probe alone answers "does this pid exist", which is not the same
    question: a process that has exited but whose parent has not reaped it yet
    keeps its pid as a zombie, and ``kill(pid, 0)`` still succeeds for it.  The
    zombie has already released its lock, so treating it as running would make
    the replacement wait out both timeouts for a process that is long gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user: it exists, we just may not signal it.
        return True

    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Return whether *pid* has exited and is only waiting to be reaped.

    Reads the state field of ``/proc/<pid>/stat``.  An unreadable or unparseable
    entry answers "not a zombie", so an unknown state leaves the process counted
    as running - the caller then waits, which is the safe direction.
    """
    try:
        text = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return False

    parsed = procfs.parse_stat(text)
    if parsed is None:
        return False

    _comm, fields = parsed

    return bool(fields) and fields[0] == 'Z'
