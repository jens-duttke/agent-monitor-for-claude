"""
Platform package imports
========================

What importing the platform package pulls in, checked in a fresh interpreter.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class PlatformImportsTest(unittest.TestCase):
    def test_the_platform_package_reaches_neither_i18n_nor_settings(self) -> None:
        # settings imports the platform package, and i18n imports settings: a
        # module the package imports that needs either closes a cycle. The
        # instance backends may, because only single_instance.py reaches them.
        script = (
            'import sys\n'
            'import agent_monitor_for_claude.platforms\n'
            "print(sorted(name for name in sys.modules if name in ('agent_monitor_for_claude.i18n', 'agent_monitor_for_claude.settings')))\n"
        )
        result = subprocess.run([sys.executable, '-c', script], cwd=_ROOT, capture_output=True, text=True, timeout=60, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')


if __name__ == '__main__':
    unittest.main()
