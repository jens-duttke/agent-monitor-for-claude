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



def _media_block(query: str) -> str:
    """The body of the stylesheet's first ``@media`` block for *query*."""
    text = _STYLESHEET.read_text(encoding='utf-8')
    start = text.index('@media ' + query + ' {')
    depth = 0
    for index in range(text.index('{', start), len(text)):
        depth += {'{': 1, '}': -1}.get(text[index], 0)
        if depth == 0:
            return text[start:index]
    raise AssertionError(query)


class NarrowLayoutTest(unittest.TestCase):
    def test_the_graph_grouping_gets_one_column_too(self) -> None:
        # Its three-column rule outranks a bare .workspace, so below 800px the
        # single visible pane kept its 420px column and the rest stayed blank.
        block = _media_block('(max-width: 799px)')
        self.assertIn('body.group-graph .workspace', block[:block.index('grid-template-columns: minmax(0, 1fr)')])


class SplitterTest(unittest.TestCase):
    def test_the_grip_stays_clear_of_the_list_scrollbar(self) -> None:
        # Reaching 5px into the list pane, it covered the scrollbar's edge.
        text = _STYLESHEET.read_text(encoding='utf-8')
        rule = text[text.index('.splitter {'):]
        grip = rule[rule.index('&::before {'):]
        left = int(re.search(r'left:\s*(-?\d+)px', grip[:grip.index('}')]).group(1))
        self.assertGreaterEqual(left, -1)


class ReducedMotionTest(unittest.TestCase):
    def test_the_search_bars_sweep_stops_too(self) -> None:
        block = _media_block('(prefers-reduced-motion: reduce)')
        self.assertIn('.search-progress.indeterminate .search-progress-fill', block)

if __name__ == '__main__':
    unittest.main()
