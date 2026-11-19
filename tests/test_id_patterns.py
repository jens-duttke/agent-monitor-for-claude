"""
Identifier patterns
===================

The patterns that validate an id before it is built into a path or stored.
"""

from __future__ import annotations

import unittest

from agent_monitor_for_claude import app, paths, settings, tasks

_UUID = '6e22e66f-6298-442a-9762-2a5b65052389'


class TrailingNewlineTest(unittest.TestCase):
    def test_no_identifier_pattern_admits_a_trailing_newline(self) -> None:
        # In Python's re, $ also matches before a final newline, so '<id>\n'
        # passed a check meant to admit nothing but the id.
        cases = [
            ('app._SESSION_UUID', app._SESSION_UUID, _UUID),
            ('paths._SESSION_UUID_PATTERN', paths._SESSION_UUID_PATTERN, _UUID),
            ('tasks._SESSION_ID_PATTERN', tasks._SESSION_ID_PATTERN, _UUID),
            ('tasks._TASK_ID_PATTERN', tasks._TASK_ID_PATTERN, 'b1x9k2'),
            ('settings._LANGUAGE_PATTERN', settings._LANGUAGE_PATTERN, 'de-AT'),
        ]
        for name, pattern, valid in cases:
            with self.subTest(name):
                self.assertTrue(pattern.match(valid))
                self.assertIsNone(pattern.match(valid + '\n'))


if __name__ == '__main__':
    unittest.main()
