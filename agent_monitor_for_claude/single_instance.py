"""
Single-Instance Guard
======================

Prevents multiple windows from running simultaneously using a named Win32
mutex.  The holder's PID and version are stored in page-file-backed shared
memory so a new instance can identify and terminate it regardless of
executable name.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import struct

from . import __version__
from .i18n import T

__all__ = ['ensure_single_instance', 'release_instance_lock']

# English last-resort text for the empty-translations degradation path (when
# every locale candidate, including en.json, failed to load and T is empty).
_DEFAULT_APP_TITLE = 'Agent Monitor for Claude'
_DEFAULT_ALREADY_RUNNING = ('Agent Monitor for Claude v{running_version} is already running.\n\n'
                            'Do you want to replace the running instance?')

_MUTEX_NAME = 'AgentMonitorForClaude_SingleInstance'
_PID_MAPPING_NAME = 'AgentMonitorForClaude_HolderPID'
_ERROR_ALREADY_EXISTS = 0xB7
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PAGE_READWRITE = 0x04
_FILE_MAP_READ = 0x0004
_FILE_MAP_WRITE = 0x0002

# Shared memory layout: 4-byte PID + null-terminated UTF-8 version string.
_SHARED_MEM_SIZE = 64

# use_last_error=True captures GetLastError() immediately after each FFI call.
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_kernel32.CreateMutexW.argtypes = [ctypes.wintypes.LPCVOID, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE

_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

_kernel32.CreateFileMappingW.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.LPCVOID, ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.LPCWSTR,
]
_kernel32.CreateFileMappingW.restype = ctypes.wintypes.HANDLE

_kernel32.OpenFileMappingW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE

_kernel32.MapViewOfFile.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t,
]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p

_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.UnmapViewOfFile.restype = ctypes.wintypes.BOOL

_kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE

_kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
_kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL

_kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD

# Handles kept alive for the process lifetime; released on exit.
_mutex_handle: int | None = None
_pid_mapping_handle: int | None = None


def _store_holder_info() -> None:
    """Store our PID and version in named, page-file-backed shared memory."""
    global _pid_mapping_handle
    _pid_mapping_handle = _kernel32.CreateFileMappingW(
        _INVALID_HANDLE, None, _PAGE_READWRITE, 0, _SHARED_MEM_SIZE, _PID_MAPPING_NAME,
    )
    if not _pid_mapping_handle:
        return

    view = _kernel32.MapViewOfFile(_pid_mapping_handle, _FILE_MAP_WRITE, 0, 0, _SHARED_MEM_SIZE)
    if not view:
        return

    version_bytes = __version__.encode('utf-8')[:_SHARED_MEM_SIZE - 5]
    payload = struct.pack(f'<I{len(version_bytes) + 1}s', os.getpid(), version_bytes + b'\x00')
    ctypes.memmove(view, payload, len(payload))
    _kernel32.UnmapViewOfFile(view)


def _read_holder_info() -> tuple[int | None, str | None]:
    """Read the PID and version of the mutex-holding instance from shared memory."""
    mapping = _kernel32.OpenFileMappingW(_FILE_MAP_READ, False, _PID_MAPPING_NAME)
    if not mapping:
        return None, None

    view = _kernel32.MapViewOfFile(mapping, _FILE_MAP_READ, 0, 0, _SHARED_MEM_SIZE)
    if not view:
        _kernel32.CloseHandle(mapping)
        return None, None

    raw = ctypes.string_at(view, _SHARED_MEM_SIZE)
    _kernel32.UnmapViewOfFile(view)
    _kernel32.CloseHandle(mapping)

    if len(raw) < 5:
        return None, None

    pid = struct.unpack('<I', raw[:4])[0]
    version = raw[4:].split(b'\x00', 1)[0].decode('utf-8', errors='replace') or None
    return pid if pid else None, version


def _terminate_pid(pid: int) -> None:
    """Terminate a process by PID and wait until it is fully dead."""
    PROCESS_TERMINATE = 0x0001
    PROCESS_SYNCHRONIZE = 0x00100000

    handle = _kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, False, pid)
    if not handle:
        return

    if not _kernel32.TerminateProcess(handle, 1):
        _kernel32.CloseHandle(handle)
        return

    _kernel32.WaitForSingleObject(handle, 5000)
    _kernel32.CloseHandle(handle)


def ensure_single_instance() -> bool:
    """Ensure only one instance runs; offer to replace a running one.

    Returns
    -------
    bool
        True if this instance may proceed, False if it should exit.
    """
    global _mutex_handle
    _mutex_handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    last_error = ctypes.get_last_error()

    # The mutex API failed outright (NULL handle, not "already exists"):
    # single-instancing cannot work. Fail open - let this instance run rather
    # than refuse to start over a rare API failure - but do not write a holder
    # record we cannot back with a held mutex, and do not treat it as a fresh
    # creation.
    if not _mutex_handle:
        _mutex_handle = None
        return True

    if last_error != _ERROR_ALREADY_EXISTS:
        _store_holder_info()
        return True

    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_TOPMOST = 0x40000
    IDYES = 6

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

    answer = ctypes.windll.user32.MessageBoxW(None, message, title, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
    if answer != IDYES:
        _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False

    # The dialog can sit open indefinitely; re-read the holder at click time. A
    # holder that exited meanwhile releases its shared-memory mapping, so a failed
    # re-read yields no PID - never terminate the earlier PID, which the OS may have
    # since recycled onto an unrelated process.
    holder_pid, _ = _read_holder_info()

    if holder_pid:
        _terminate_pid(holder_pid)
    _kernel32.CloseHandle(_mutex_handle)

    _mutex_handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)

    # Confirm this instance actually became the sole owner. If the mutex still
    # pre-exists, the previous holder outlived the terminate (it was elevated, or
    # the wait timed out, or its PID was never recorded) - the replace failed, so
    # leave the old instance running and exit rather than start a second window
    # and hijack the holder record.
    if not _mutex_handle or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        if _mutex_handle:
            _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False

    _store_holder_info()
    return True


def release_instance_lock() -> None:
    """Release the mutex and shared memory so a new instance can start."""
    global _mutex_handle, _pid_mapping_handle

    if _mutex_handle:
        _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None

    if _pid_mapping_handle:
        _kernel32.CloseHandle(_pid_mapping_handle)
        _pid_mapping_handle = None
