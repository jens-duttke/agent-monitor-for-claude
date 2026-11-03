"""
Tests that the PyInstaller spec bundles exactly the UI files the page loads.

The spec lists every shipped UI asset by name, and the page pulls its
controller scripts in through ``boot-scripts.js``.  A script added to one list
and not the other builds without complaint and fails only in the packaged
window - as a blank page - so the two are compared here.  The browser
preview's ``dev-mock.js`` must never ship.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_UI_DIR = _ROOT / 'agent_monitor_for_claude' / 'ui'


def _bundled_ui_files() -> set[str]:
    spec = (_ROOT / 'agent_monitor_for_claude.spec').read_text(encoding='utf-8')
    return set(re.findall(r"\('agent_monitor_for_claude/ui/([\w.-]+)',", spec))


def _loaded_scripts() -> list[str]:
    source = (_UI_DIR / 'boot-scripts.js').read_text(encoding='utf-8')
    listing = re.search(r'var scripts = \[([^\]]*)\]', source)
    if listing is None:
        raise AssertionError('boot-scripts.js no longer holds a `var scripts = [...]` list')

    return re.findall(r"'([\w.-]+\.js)'", listing.group(1))


class SpecDatasTest(unittest.TestCase):
    def test_every_loaded_script_is_bundled(self) -> None:
        missing = [name for name in _loaded_scripts() if name not in _bundled_ui_files()]
        self.assertEqual(missing, [])

    def test_the_page_and_its_bootstraps_are_bundled(self) -> None:
        self.assertLessEqual({'index.html', 'index.css', 'boot.js', 'boot-scripts.js'}, _bundled_ui_files())

    def test_nothing_is_bundled_that_the_page_does_not_load(self) -> None:
        expected = set(_loaded_scripts()) | {'index.html', 'index.css', 'boot.js', 'boot-scripts.js'}
        self.assertEqual(_bundled_ui_files() - expected, set())

    def test_the_mock_data_never_ships(self) -> None:
        self.assertNotIn('dev-mock.js', _bundled_ui_files())

    def test_every_bundled_file_exists(self) -> None:
        missing = [name for name in sorted(_bundled_ui_files()) if not (_UI_DIR / name).is_file()]
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
