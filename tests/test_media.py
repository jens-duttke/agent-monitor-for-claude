"""Tests for the images read out of a transcript: where they are, one of them on request, and nothing past what was listed."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import app, transcript
from agent_monitor_for_claude.app import _MonitorApi
from agent_monitor_for_claude.paths import SessionRoot
from agent_monitor_for_claude.transcript import hook_failures, media_image, media_index

_PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nfake-png-bytes').decode()
_JPEG = base64.b64encode(b'\xff\xd8\xff fake jpeg').decode()


def _line(entry: dict) -> str:
    return json.dumps(entry, separators=(',', ':')) + '\n'


def _read_call(call_id: str, file_path: str) -> str:
    return _line({'type': 'assistant', 'timestamp': '2026-11-06T10:00:00Z', 'message': {'content': [
        {'type': 'tool_use', 'id': call_id, 'name': 'Read', 'input': {'file_path': file_path}},
    ]}})


def _tool_image(call_id: str, media_type: str, data: str, at: str = '2026-11-06T10:00:01Z') -> str:
    return _line({'type': 'user', 'timestamp': at, 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': call_id, 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data}}]},
    ]}})


def _pasted(media_type: str, data: str) -> str:
    return _line({'type': 'user', 'timestamp': '2026-11-06T11:00:00Z', 'message': {'content': [
        {'type': 'text', 'text': 'look at this'},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data}},
    ]}})


class MediaIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        transcript._media_cache.clear()
        self.addCleanup(transcript._media_cache.clear)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'session.jsonl'

    def _write(self, *lines: str, mode: str = 'w') -> None:
        with open(self.path, mode, encoding='utf-8', newline='\n') as handle:
            handle.write(''.join(lines))

    def test_lists_tool_and_pasted_images_newest_first_without_their_data(self) -> None:
        self._write(_read_call('t1', 'C:\\docs\\shot.png'), _tool_image('t1', 'image/png', _PNG), _pasted('image/jpeg', _JPEG))

        images = media_index(self.path)

        self.assertEqual([(item['origin'], item['tool'], item['path'], item['media_type']) for item in images], [
            ('pasted', '', '', 'image/jpeg'), ('tool', 'Read', 'C:\\docs\\shot.png', 'image/png'),
        ])
        self.assertTrue(all('data' not in item for item in images))
        self.assertEqual(images[1]['at'], 1793959201.0)

    def test_an_image_is_handed_over_only_for_an_id_the_index_holds(self) -> None:
        self._write(_read_call('t1', 'a.png'), _tool_image('t1', 'image/png', _PNG))
        listed = media_index(self.path)

        self.assertEqual(media_image(self.path, listed[0]['id']), {'media_type': 'image/png', 'data': _PNG})
        # Any other place in the file - the call line itself, a made-up offset - is refused.
        self.assertIsNone(media_image(self.path, '0.0'))
        self.assertIsNone(media_image(self.path, listed[0]['id'] + '1'))
        self.assertIsNone(media_image(self.path, 'garbage'))

    def test_other_formats_and_broken_data_are_left_out(self) -> None:
        self._write(_pasted('image/svg+xml', base64.b64encode(b'<svg onload=alert(1)>').decode()), _pasted('image/png', '"><script>'))

        self.assertEqual(media_index(self.path)[0]['media_type'], 'image/png')
        broken = media_index(self.path)[0]
        self.assertIsNone(media_image(self.path, broken['id']))
        self.assertEqual(len(media_index(self.path)), 1)

    def test_an_appended_image_is_found_and_a_half_written_line_waits(self) -> None:
        self._write(_pasted('image/png', _PNG))
        self.assertEqual(len(media_index(self.path)), 1)

        partial = _pasted('image/jpeg', _JPEG)
        self._write(partial[:-20], mode='a')
        self.assertEqual(len(media_index(self.path)), 1)
        self._write(partial[-20:], mode='a')
        self.assertEqual([item['media_type'] for item in media_index(self.path)], ['image/jpeg', 'image/png'])

    def test_an_unreadable_file_has_no_images(self) -> None:
        self.assertEqual(media_index(Path(self.directory.name) / 'missing.jsonl'), [])


class MediaBridgeTest(unittest.TestCase):
    """The bridge reads only for a UUID session in an available root; an agent goes to its own reader."""

    _SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'

    def _root(self) -> SessionRoot:
        return SessionRoot(origin='local', label=None, config_dir=Path('cfg'), proc_dir=None, claude_temp_dir=Path('tmp'))

    def test_malformed_requests_never_reach_a_reader(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'session_media') as session_mock, mock.patch.object(app, 'session_media_image') as image_mock:
            self.assertIsNone(api.get_session_media('not-a-uuid', 'D:\\p'))
            self.assertIsNone(api.get_session_media(self._SESSION, ''))
            self.assertIsNone(api.get_media_image(self._SESSION, 'D:\\p', 'local', '', ''))
            with mock.patch.object(app, 'root_for_origin', return_value=None):
                self.assertIsNone(api.get_session_media(self._SESSION, 'D:\\p', 'gone:X'))

        session_mock.assert_not_called()
        image_mock.assert_not_called()

    def test_hook_failures_are_read_only_for_a_uuid_session(self) -> None:
        api = _MonitorApi()
        with mock.patch.object(app, 'session_hook_failures', return_value=[]) as failures_mock, \
             mock.patch.object(app, 'root_for_origin', return_value=self._root()):
            self.assertIsNone(api.get_hook_failures('not-a-uuid', 'D:\\p'))
            self.assertEqual(api.get_hook_failures(self._SESSION, 'D:\\p'), [])

        failures_mock.assert_called_once()

    def test_a_session_and_an_agent_are_routed_to_their_readers(self) -> None:
        api = _MonitorApi()
        root = self._root()
        with mock.patch.object(app, 'root_for_origin', return_value=root), \
             mock.patch.object(app, 'session_media', return_value={'images': []}) as session_mock, \
             mock.patch.object(app, 'agent_media', return_value={'images': []}) as agent_mock, \
             mock.patch.object(app, 'agent_media_image', return_value=None) as agent_image_mock:
            api.get_session_media(self._SESSION, 'D:\\p')
            api.get_session_media(self._SESSION, 'D:\\p', 'local', 'a1b2')
            api.get_media_image(self._SESSION, 'D:\\p', 'local', 'a1b2', '12.0')

        session_mock.assert_called_once_with(root, self._SESSION, 'D:\\p')
        agent_mock.assert_called_once_with(root, self._SESSION, 'a1b2')
        agent_image_mock.assert_called_once_with(root, self._SESSION, 'a1b2', '12.0')


class HookFailuresTest(unittest.TestCase):
    """Every failed hook, whole, with the call that set it off; only failure lines and their calls are read."""

    def test_each_failure_names_its_command_output_and_triggering_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            lines = [
                _line({'type': 'assistant', 'timestamp': '2026-11-06T10:00:00Z', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'npm test', 'description': 'Run tests'}},
                ]}}),
                _line({'type': 'attachment', 'timestamp': '2026-11-06T10:00:05Z', 'attachment': {
                    'type': 'hook_non_blocking_error', 'hookEvent': 'PostToolUse', 'hookName': 'PostToolUse:Bash', 'command': 'python hooks/check.py',
                    'exitCode': 7, 'durationMs': 2150, 'stderr': 'Failed with non-blocking status code: boom\nsecond line', 'stdout': 'partial',
                    'toolUseID': 'toolu_1',
                }}),
                _line({'type': 'attachment', 'timestamp': '2026-11-06T10:01:00Z', 'attachment': {
                    'type': 'hook_non_blocking_error', 'hookEvent': 'UserPromptSubmit', 'hookName': 'UserPromptSubmit', 'command': 'x',
                    'exitCode': 1, 'durationMs': 10, 'stderr': 'Failed with non-blocking status code: No stderr output', 'stdout': '', 'toolUseID': '',
                }}),
            ]
            path.write_text(''.join(lines), encoding='utf-8')

            failures = hook_failures(path)

        self.assertEqual([failure['hook'] for failure in failures], ['UserPromptSubmit', 'PostToolUse:Bash'])
        newest, oldest = failures
        self.assertEqual((newest['stderr'], newest['tool'], newest['tool_text']), ('', '', ''))
        self.assertEqual(oldest, {
            'at': 1793959205.0, 'event': 'PostToolUse', 'hook': 'PostToolUse:Bash', 'command': 'python hooks/check.py', 'exit_code': 7,
            'duration_ms': 2150, 'stderr': 'Failed with non-blocking status code: boom\nsecond line', 'stdout': 'partial',
            'tool': 'Bash', 'tool_text': 'npm test',
        })

    def test_a_missing_file_has_no_failures(self) -> None:
        self.assertEqual(hook_failures(Path('does-not-exist.jsonl')), [])
