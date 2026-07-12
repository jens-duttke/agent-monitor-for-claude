"""
Entry Point
===========

Launches Agent Monitor for Claude.  Uses absolute imports so the module works
both as ``python -m agent_monitor_for_claude`` and inside a PyInstaller bundle.

Flags:
    --verbose   Print system and runtime diagnostics to the console.
"""
from __future__ import annotations

import sys

from agent_monitor_for_claude.app import run
from agent_monitor_for_claude.single_instance import ensure_single_instance, release_instance_lock
from agent_monitor_for_claude.verbose import print_startup_diagnostics, setup_console


def main() -> None:
    """Guard against a second instance, then run the window."""
    verbose = '--verbose' in sys.argv
    if verbose:
        setup_console()
        print_startup_diagnostics()

    if not ensure_single_instance():
        return

    try:
        run(verbose)
    finally:
        release_instance_lock()


if __name__ == '__main__':
    main()
