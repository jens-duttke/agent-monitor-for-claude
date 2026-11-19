"""
Stylesheet tests
================

Rules of the UI stylesheet that one of the two browser engines enforces and the
other does not, so a change that looks right on the system it was made on can
break the other one silently.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_STYLESHEET = Path(__file__).resolve().parent.parent / 'agent_monitor_for_claude' / 'ui' / 'index.css'

# One declaration of either spelling, with its value.
_DECLARATION = re.compile(r'(?m)^\s*(-webkit-)?user-select\s*:\s*([^;]+);')


class UserSelectTest(unittest.TestCase):
    def test_every_user_select_has_its_webkit_twin(self) -> None:
        # WebKitGTK reads only the prefixed spelling, and inherits it: the body's
        # '-webkit-user-select: none' reaches every element whose opt-in to
        # selection is written unprefixed only, so nothing is selectable on Linux.
        declarations = [(bool(match.group(1)), match.group(2).strip()) for match in _DECLARATION.finditer(_STYLESHEET.read_text(encoding='utf-8'))]
        unpaired = []
        for index, (prefixed, value) in enumerate(declarations):
            if prefixed:
                continue
            if index == 0 or declarations[index - 1] != (True, value):
                unpaired.append(value)

        self.assertEqual(unpaired, [])


def _z_index(selector: str) -> int:
    """The z-index of the top-level rule for *selector*."""
    text = _STYLESHEET.read_text(encoding='utf-8')
    match = re.search(r'(?m)^' + re.escape(selector) + r' \{[^}]*?z-index:\s*(\d+)', text)
    assert match, selector
    return int(match.group(1))


class FloatingLayerTest(unittest.TestCase):
    def test_a_menu_opened_in_a_dialog_shows_above_it_and_below_the_tooltip(self) -> None:
        # The copy menu over the settings dialog's selectable path was painted
        # under the modal overlay - open, invisible, and closed by the next click.
        self.assertGreater(_z_index('.menu'), _z_index('.modal-overlay'))
        self.assertLess(_z_index('.menu'), _z_index('.tooltip'))


if __name__ == '__main__':
    unittest.main()
