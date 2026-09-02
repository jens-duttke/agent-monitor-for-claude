"""
Single-Instance Guard
======================

Prevents multiple windows from running simultaneously, and offers to replace an
instance that is already running.  Both systems keep the holder's PID and
version so the replacement can identify it regardless of executable name, but
they keep it in different places: Windows in a named mutex plus a page-file-
backed shared-memory block, Linux in a ``flock``-ed file in the session's
runtime directory.  That lock file is the one lasting trace this guard leaves on
disk, and only on Linux - see ``PRIVACY.md``.

Kept out of :mod:`agent_monitor_for_claude.platforms` on purpose: the guard
needs the translations, and ``i18n`` would close an import cycle back into the
platform package.
"""
from __future__ import annotations

import sys

if sys.platform == 'win32':
    from .platforms.instance_win32 import ensure_single_instance, release_instance_lock
else:
    from .platforms.instance_linux import ensure_single_instance, release_instance_lock

__all__ = ['ensure_single_instance', 'release_instance_lock']
