"""
Tests for the entry point's start-up order.

Two things here are load-bearing enough to pin.  The GUI environment is prepared
before anything else can touch the toolkit: on Linux the instance guard's replace
dialog and the diagnostics both reach GTK, and in the environment a Snap-packaged
terminal hands over, that first touch kills the process outright - so an
innocent-looking reordering of ``main`` would break start-up completely, and
silently on the machine of whoever did not test there.

The other is the Python version check, which is correct only where it stands:
above the package imports, and with nothing importable ahead of it in the
package's own ``__init__``.  Moved below them - which is where an import sorter
puts it - it never runs, because the imports it exists to pre-empt fail first.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import __main__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = _REPO_ROOT / 'agent_monitor_for_claude'


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


class VersionGuardTest(unittest.TestCase):
    def test_an_old_python_is_told_its_own_version_instead_of_an_import_error(self) -> None:
        # The version cannot be faked in-process (the module is long imported), so a
        # subprocess takes the real start-up route with a rewritten ``sys.version_info``.
        script = (
            'import runpy, sys\n'
            "sys.version_info = (3, 9, 23, 'final', 0)\n"
            "runpy.run_module('agent_monitor_for_claude', run_name='__main__')\n"
        )
        result = subprocess.run([sys.executable, '-c', script], cwd=_REPO_ROOT, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('3.10', result.stderr)
        self.assertIn('3.9.23', result.stderr)
        self.assertIn(sys.executable, result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_the_check_stands_above_the_package_imports(self) -> None:
        body = ast.parse((_PACKAGE_DIR / '__main__.py').read_text(encoding='utf-8')).body

        guard = -1
        package_import = -1
        for index, node in enumerate(body):
            if guard < 0 and isinstance(node, ast.If) and _reads_version_info(node.test):
                guard = index
            if package_import < 0 and isinstance(node, ast.ImportFrom) and (node.module or '').startswith('agent_monitor_for_claude'):
                package_import = index

        self.assertGreaterEqual(guard, 0, 'the version check is gone')
        self.assertGreaterEqual(package_import, 0)
        self.assertLess(guard, package_import)

    def test_the_package_init_imports_nothing_the_check_has_to_pre_empt(self) -> None:
        # "python -m agent_monitor_for_claude" runs __init__.py first, so an import
        # added there would reach pywebview before the check in __main__ ever runs.
        tree = ast.parse((_PACKAGE_DIR / '__init__.py').read_text(encoding='utf-8'))

        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or '')

        self.assertEqual(imported, ['__future__'])


def _reads_version_info(test: ast.expr) -> bool:
    """Report whether an ``if`` test looks at ``sys.version_info``."""
    for node in ast.walk(test):
        if isinstance(node, ast.Attribute) and node.attr == 'version_info':
            return True

    return False


if __name__ == '__main__':
    unittest.main()
