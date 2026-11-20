"""
Update check tests
==================

The release lookup runs only while its setting is on, never blocks the caller,
reads only a plain release number from the answer, and keeps a known answer
through a later failure.  No test touches the network: the request is always a
stand-in.
"""
from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from agent_monitor_for_claude import __version__
from agent_monitor_for_claude import update_check as update_mod
from agent_monitor_for_claude.settings import Settings


def _settings(on: bool) -> mock._patch:
    return mock.patch.object(update_mod, 'current_settings', return_value=Settings(update_check=on))


def _wait_for_check() -> None:
    for thread in threading.enumerate():
        if thread.name == 'update-check':
            thread.join(timeout=5)


class UpdateStatusTest(unittest.TestCase):

    def setUp(self) -> None:
        update_mod._reset()
        self.addCleanup(update_mod._reset)

    def test_off_reports_the_version_and_never_fetches(self) -> None:
        fetch = mock.Mock(return_value='9.9.9')
        with _settings(False):
            status = update_mod.update_status(fetch)

        self.assertEqual(status, {'enabled': False, 'current': __version__})
        fetch.assert_not_called()

    def test_the_default_setting_is_off(self) -> None:
        self.assertFalse(Settings().update_check)

    def test_first_call_starts_a_check_and_answers_at_once(self) -> None:
        release = threading.Event()

        def fetch() -> str:
            release.wait(5)
            return '9.9.9'

        with _settings(True):
            status = update_mod.update_status(fetch)
            self.assertTrue(status['pending'])
            self.assertIsNone(status['latest'])
            release.set()
            _wait_for_check()
            status = update_mod.update_status(fetch)

        self.assertEqual(status['latest'], '9.9.9')
        self.assertFalse(status['pending'])
        self.assertFalse(status['failed'])
        self.assertIsInstance(status['checked_at'], float)

    def test_a_fresh_answer_is_not_asked_again(self) -> None:
        fetch = mock.Mock(return_value='9.9.9')
        with _settings(True):
            update_mod.update_status(fetch)
            _wait_for_check()
            update_mod.update_status(fetch)
            update_mod.update_status(fetch)

        self.assertEqual(fetch.call_count, 1)

    def test_a_due_check_runs_again(self) -> None:
        fetch = mock.Mock(side_effect=['9.9.9', '9.9.10'])
        with _settings(True):
            update_mod.update_status(fetch)
            _wait_for_check()
            with mock.patch.object(update_mod.time, 'monotonic', return_value=update_mod.time.monotonic() + update_mod._CHECK_INTERVAL_SECONDS + 1):
                update_mod.update_status(fetch)
                _wait_for_check()
                status = update_mod.update_status(fetch)

        self.assertEqual(status['latest'], '9.9.10')

    def test_a_failure_keeps_the_known_answer_and_waits_before_retrying(self) -> None:
        fetch = mock.Mock(side_effect=['9.9.9', OSError('offline'), '9.9.9'])
        with _settings(True):
            update_mod.update_status(fetch)
            _wait_for_check()
            later = update_mod.time.monotonic() + update_mod._CHECK_INTERVAL_SECONDS + 1
            with mock.patch.object(update_mod.time, 'monotonic', return_value=later):
                update_mod.update_status(fetch)
                _wait_for_check()
                status = update_mod.update_status(fetch)

        self.assertEqual(status['latest'], '9.9.9')
        self.assertTrue(status['failed'])
        self.assertEqual(fetch.call_count, 2)

    def test_an_answer_without_a_version_counts_as_a_failure(self) -> None:
        with _settings(True):
            update_mod.update_status(lambda: None)
            _wait_for_check()
            status = update_mod.update_status(lambda: None)

        self.assertIsNone(status['latest'])
        self.assertTrue(status['failed'])


class ParseReleaseTest(unittest.TestCase):

    def test_reads_the_tag_with_or_without_a_v(self) -> None:
        self.assertEqual(update_mod._parse_release(json.dumps({'tag_name': 'v1.2.3'}).encode()), '1.2.3')
        self.assertEqual(update_mod._parse_release(json.dumps({'tag_name': '1.2.3'}).encode()), '1.2.3')

    def test_anything_but_a_plain_release_number_is_refused(self) -> None:
        for tag in ('v1.2', 'v1.2.3-beta', 'v1.2.3"><script>', '', None, 123, ['v1.2.3']):
            with self.subTest(tag=tag):
                self.assertIsNone(update_mod._parse_release(json.dumps({'tag_name': tag}).encode()))

    def test_a_body_that_is_not_a_release_record_is_refused(self) -> None:
        for body in (b'', b'not json', b'[]', b'"v1.2.3"', b'\xff\xfe'):
            with self.subTest(body=body):
                self.assertIsNone(update_mod._parse_release(body))


class FetchLatestTest(unittest.TestCase):

    def _response(self, body: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = lambda limit: body[:limit]
        return response

    def test_asks_the_fixed_address_anonymously_with_a_timeout(self) -> None:
        body = json.dumps({'tag_name': 'v2.0.0', 'body': 'notes'}).encode()
        with mock.patch.object(update_mod.urllib.request, 'urlopen', return_value=self._response(body)) as urlopen:
            self.assertEqual(update_mod._fetch_latest(), '2.0.0')

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, update_mod.LATEST_RELEASE_API)
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertNotIn('Authorization', request.headers)
        self.assertNotIn('Cookie', request.headers)
        self.assertEqual(urlopen.call_args.kwargs['timeout'], update_mod._TIMEOUT_SECONDS)

    def test_an_oversized_answer_is_refused(self) -> None:
        body = json.dumps({'tag_name': 'v2.0.0', 'body': 'x' * (update_mod._MAX_RESPONSE_BYTES + 10)}).encode()
        with mock.patch.object(update_mod.urllib.request, 'urlopen', return_value=self._response(body)):
            self.assertIsNone(update_mod._fetch_latest())


if __name__ == '__main__':
    unittest.main()
