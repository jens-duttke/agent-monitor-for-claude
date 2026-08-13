"""
Tests for the in-memory last-seen-alive memory.

``last_seen`` is what lets the snapshot measure an ended session's retention
window from the moment it ended rather than from its last transcript entry.
These tests cover the three states that matter - never seen, just seen, seen a
while ago - plus the pruning that keeps the dict bounded and the guards against
empty keys.
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_monitor_for_claude import last_seen


class LastSeenTest(unittest.TestCase):
    def setUp(self) -> None:
        last_seen.prune_last_seen([])

    def tearDown(self) -> None:
        last_seen.prune_last_seen([])

    def test_unknown_session_has_no_sighting(self) -> None:
        # None (not 0.0) is the "never seen" signal the caller falls back on.
        self.assertIsNone(last_seen.seconds_since_alive('windows', 'unseen'))

    def test_sighting_ages_with_the_monotonic_clock(self) -> None:
        with mock.patch.object(last_seen.time, 'monotonic', return_value=1000.0):
            last_seen.note_alive('windows', 'a')

        with mock.patch.object(last_seen.time, 'monotonic', return_value=1042.0):
            self.assertEqual(last_seen.seconds_since_alive('windows', 'a'), 42.0)

    def test_later_sighting_replaces_the_earlier_one(self) -> None:
        with mock.patch.object(last_seen.time, 'monotonic', return_value=1000.0):
            last_seen.note_alive('windows', 'a')
        with mock.patch.object(last_seen.time, 'monotonic', return_value=1500.0):
            last_seen.note_alive('windows', 'a')

        with mock.patch.object(last_seen.time, 'monotonic', return_value=1600.0):
            self.assertEqual(last_seen.seconds_since_alive('windows', 'a'), 100.0)

    def test_sightings_are_kept_apart_by_origin(self) -> None:
        # A session id is a UUID and unique on its own, but the key pairs it with
        # its root exactly like every other cross-root lookup - one root's
        # sighting must never answer for another's.
        with mock.patch.object(last_seen.time, 'monotonic', return_value=1000.0):
            last_seen.note_alive('windows', 'a')

        self.assertIsNone(last_seen.seconds_since_alive('Ubuntu', 'a'))

    def test_empty_keys_are_ignored(self) -> None:
        last_seen.note_alive('', 'a')
        last_seen.note_alive('windows', '')

        self.assertIsNone(last_seen.seconds_since_alive('', 'a'))
        self.assertIsNone(last_seen.seconds_since_alive('windows', ''))

    def test_prune_drops_only_sessions_that_left_the_registry(self) -> None:
        last_seen.note_alive('windows', 'kept')
        last_seen.note_alive('windows', 'gone')

        last_seen.prune_last_seen([('windows', 'kept')])

        self.assertIsNotNone(last_seen.seconds_since_alive('windows', 'kept'))
        self.assertIsNone(last_seen.seconds_since_alive('windows', 'gone'))

    def test_a_backwards_clock_reads_as_just_seen(self) -> None:
        # time.monotonic cannot go backwards, but the floor keeps a negative age
        # (which would read as "in the future") impossible regardless.
        with mock.patch.object(last_seen.time, 'monotonic', return_value=1000.0):
            last_seen.note_alive('windows', 'a')

        with mock.patch.object(last_seen.time, 'monotonic', return_value=900.0):
            self.assertEqual(last_seen.seconds_since_alive('windows', 'a'), 0.0)


if __name__ == '__main__':
    unittest.main()
