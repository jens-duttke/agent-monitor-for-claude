"""
Tests for the entry point's start-up order.

Only one thing here is load-bearing enough to pin: the GUI environment is
prepared before anything else can touch the toolkit.  On Linux the instance
guard's replace dialog and the diagnostics both reach GTK, and in the
environment a Snap-packaged terminal hands over, that first touch kills the
process outright - so an innocent-looking reordering of ``main`` would break
start-up completely, and silently on the machine of whoever did not test there.
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import __main__


class StartupOrderTest(unittest.TestCase):
    def _run_main(self, argv: list[str]) -> list[str]:
        """Run ``main`` with every step recorded instead of performed."""
        calls: list[str] = []

        def record(name: str, result: object = None):
            def inner(*_args: object, **_kwargs: object) -> object:
                calls.append(name)
                return result
            return inner

        with mock.patch.object(__main__.sys, 'argv', argv), \
             mock.patch.object(__main__, 'prepare_gui_environment', record('prepare')), \
             mock.patch.object(__main__, 'setup_console', record('console')), \
             mock.patch.object(__main__, 'print_startup_diagnostics', record('diagnostics')), \
             mock.patch.object(__main__, 'ensure_single_instance', record('guard', True)), \
             mock.patch.object(__main__, 'run', record('run')), \
             mock.patch.object(__main__, 'release_instance_lock', record('release')):
            __main__.main()

        return calls

    def test_the_environment_is_prepared_before_anything_reaches_the_toolkit(self) -> None:
        self.assertEqual(self._run_main(['prog']), ['prepare', 'guard', 'run', 'release'])

    def test_that_holds_with_verbose_diagnostics_too(self) -> None:
        # The diagnostics read the toolkit's version, so they must not come first.
        self.assertEqual(
            self._run_main(['prog', '--verbose']),
            ['prepare', 'console', 'diagnostics', 'guard', 'run', 'release'],
        )

    def test_a_refused_second_instance_still_prepared_the_environment(self) -> None:
        # The guard's replace dialog is itself a GTK window on Linux.
        calls: list[str] = []
        with mock.patch.object(__main__.sys, 'argv', ['prog']), \
             mock.patch.object(__main__, 'prepare_gui_environment', lambda: calls.append('prepare')), \
             mock.patch.object(__main__, 'ensure_single_instance', lambda: calls.append('guard') or False), \
             mock.patch.object(__main__, 'run') as run:
            __main__.main()

        self.assertEqual(calls, ['prepare', 'guard'])
        run.assert_not_called()

    def test_the_lock_is_released_even_when_the_window_fails(self) -> None:
        with mock.patch.object(__main__.sys, 'argv', ['prog']), \
             mock.patch.object(__main__, 'prepare_gui_environment'), \
             mock.patch.object(__main__, 'ensure_single_instance', return_value=True), \
             mock.patch.object(__main__, 'run', side_effect=RuntimeError('boom')), \
             mock.patch.object(__main__, 'release_instance_lock') as release:
            with self.assertRaises(RuntimeError):
                __main__.main()

        release.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
