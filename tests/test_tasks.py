"""Tests for the background-task output boundary (tasks module).

Guards the new content-reading surface: enumeration is metadata-only, output is
read only for a valid session + task id, every path is confined to the session's
task-output directory, and only the tail of a large file is returned.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude.paths import cwd_to_slug
from agent_monitor_for_claude.tasks import list_tasks, read_task_output, _parse_redirect_target, _wsl_to_windows

_SESSION = '6e22e66f-6298-442a-9762-2a5b65052389'
_CWD = r'D:\WebDev\vs-edge264'


class TasksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        patcher = mock.patch('tempfile.gettempdir', return_value=self._tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._dir = Path(self._tmp) / 'claude' / cwd_to_slug(_CWD) / _SESSION / 'tasks'
        self._dir.mkdir(parents=True, exist_ok=True)

        # A separate config dir so the transcript (used only for task labels)
        # resolves into a temp tree, not the real ~/.claude.
        self._config = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self._config})
        env.start()
        self.addCleanup(env.stop)
        self._transcript = Path(self._config) / 'projects' / cwd_to_slug(_CWD) / f'{_SESSION}.jsonl'
        self._transcript.parent.mkdir(parents=True, exist_ok=True)

    def _write_transcript(self, task_id: str, description: str, command: str = 'run it') -> None:
        use = {
            'type': 'assistant',
            'message': {'content': [{
                'type': 'tool_use', 'name': 'Bash', 'id': 'toolu_1',
                'input': {'command': command, 'description': description, 'run_in_background': True},
            }]},
        }
        result = {
            'type': 'user',
            'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': 'toolu_1',
                'content': f'Command running in background with ID: {task_id}. Output is being written to: x.output.',
            }]},
        }
        lines = [json.dumps(use, ensure_ascii=False), json.dumps(result, ensure_ascii=False)]
        self._transcript.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='')

    def _write(self, name: str, text: str, *, age_seconds: float | None = None) -> Path:
        path = self._dir / name
        # newline='' so Windows does not translate \n to \r\n - the output is
        # read back byte-for-byte, so the test asserts on exactly what is written.
        path.write_text(text, encoding='utf-8', newline='')
        if age_seconds is not None:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))
        return path

    def test_lists_recent_tasks_freshest_first(self) -> None:
        self._write('older.output', 'a', age_seconds=120)
        self._write('newer.output', 'bb', age_seconds=5)
        tasks, total = list_tasks(_SESSION, _CWD)
        self.assertEqual([task.task_id for task in tasks], ['newer', 'older'])
        self.assertEqual(total, 2)
        self.assertEqual(tasks[0].size_bytes, 2)

    def test_skips_stale_tasks(self) -> None:
        self._write('fresh.output', 'x', age_seconds=10)
        self._write('ancient.output', 'y', age_seconds=10000)
        tasks, total = list_tasks(_SESSION, _CWD, recent_seconds=3600)
        self.assertEqual([task.task_id for task in tasks], ['fresh'])
        self.assertEqual(total, 1)

    def test_ignores_non_output_and_bad_ids(self) -> None:
        self._write('real.output', 'x', age_seconds=1)
        self._write('notes.txt', 'x', age_seconds=1)
        self._write('bad id.output', 'x', age_seconds=1)
        tasks, _total = list_tasks(_SESSION, _CWD)
        self.assertEqual([task.task_id for task in tasks], ['real'])

    def test_cap_reports_total(self) -> None:
        for index in range(30):
            self._write(f't{index:02d}.output', 'x', age_seconds=index)
        tasks, total = list_tasks(_SESSION, _CWD, max_tasks=25)
        self.assertEqual(len(tasks), 25)
        self.assertEqual(total, 30)

    def test_labels_from_transcript(self) -> None:
        self._write('mytask1.output', 'x', age_seconds=3)
        self._write_transcript('mytask1', 'Run the big regression')
        tasks, _total = list_tasks(_SESSION, _CWD)
        self.assertEqual(tasks[0].task_id, 'mytask1')
        self.assertEqual(tasks[0].label, 'Run the big regression')

    def test_label_empty_without_transcript_entry(self) -> None:
        self._write('orphan9.output', 'x', age_seconds=3)
        tasks, _total = list_tasks(_SESSION, _CWD)
        self.assertEqual(tasks[0].label, '')

    def test_non_uuid_session_returns_empty(self) -> None:
        self.assertEqual(list_tasks('not-a-uuid', _CWD), ([], 0))

    def test_missing_directory_returns_empty(self) -> None:
        other = '11111111-2222-3333-4444-555555555555'
        self.assertEqual(list_tasks(other, _CWD), ([], 0))

    def test_reads_output_text(self) -> None:
        self._write('abc123.output', 'hello\nworld\n', age_seconds=1)
        self.assertEqual(read_task_output(_SESSION, _CWD, 'abc123'), 'hello\nworld\n')

    def test_reads_only_tail_of_large_file(self) -> None:
        lines = [f'line-{index:06d}' for index in range(20000)]
        self._write('big.output', '\n'.join(lines) + '\n', age_seconds=1)
        text = read_task_output(_SESSION, _CWD, 'big', max_bytes=4096)
        self.assertTrue(text.startswith('…\n'))
        self.assertLessEqual(len(text.encode('utf-8')), 4096 + 4)
        self.assertIn('line-019999', text)
        self.assertNotIn('line-000000', text)

    def test_read_rejects_traversal_task_id(self) -> None:
        # A dot or separator is not in the allowed id set, so a crafted id can
        # never build a path outside the tasks directory.
        for crafted in ('../../secret', '..\\..\\secret', 'a/b', 'a.b'):
            self.assertIsNone(read_task_output(_SESSION, _CWD, crafted))

    def test_read_rejects_non_uuid_session(self) -> None:
        self.assertIsNone(read_task_output('not-a-uuid', _CWD, 'abc123'))

    def test_read_missing_file_returns_none(self) -> None:
        self.assertIsNone(read_task_output(_SESSION, _CWD, 'doesnotexist'))

    def test_follows_redirect_into_scratchpad(self) -> None:
        # The capture file is empty because the command redirected its output to
        # a file in the session scratchpad; that file's content is shown instead.
        self._write('redir01.output', '', age_seconds=5)
        scratch = Path(self._tmp) / 'claude' / cwd_to_slug(_CWD) / _SESSION / 'scratchpad'
        scratch.mkdir(parents=True, exist_ok=True)
        log = scratch / 'run.log'
        log.write_text('progress 42/100\n', encoding='utf-8', newline='')
        self._write_transcript('redir01', 'Big run', command=f'bash job.sh > {log} 2>&1')

        self.assertEqual(read_task_output(_SESSION, _CWD, 'redir01'), 'progress 42/100\n')
        tasks, _total = list_tasks(_SESSION, _CWD)
        self.assertEqual(tasks[0].size_bytes, len('progress 42/100\n'))

    def test_follows_relative_redirect_into_cwd(self) -> None:
        # A relative redirect target resolves against the session cwd (where the
        # task ran), not the monitor's cwd, and is read when it lands in-bounds.
        realcwd = tempfile.mkdtemp()
        slug = cwd_to_slug(realcwd)
        tasks = Path(self._tmp) / 'claude' / slug / _SESSION / 'tasks'
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / 'relc01.output').write_text('', encoding='utf-8', newline='')
        (Path(realcwd) / 'build.log').write_text('compiling...\n', encoding='utf-8', newline='')

        transcript = Path(self._config) / 'projects' / slug / f'{_SESSION}.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        use = {'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'name': 'Bash', 'id': 'toolu_r',
            'input': {'command': 'bash job.sh > build.log 2>&1', 'description': 'Rel', 'run_in_background': True},
        }]}}
        result = {'type': 'user', 'message': {'content': [{
            'type': 'tool_result', 'tool_use_id': 'toolu_r',
            'content': 'Command running in background with ID: relc01. Output is being written to: x.output.',
        }]}}
        transcript.write_text(json.dumps(use) + '\n' + json.dumps(result) + '\n', encoding='utf-8', newline='')

        self.assertEqual(read_task_output(_SESSION, realcwd, 'relc01'), 'compiling...\n')

    def test_redirect_outside_roots_is_ignored(self) -> None:
        # A redirect target outside the scratchpad and project dir must never be
        # read, even though the file exists and the command names it.
        self._write('redir02.output', '', age_seconds=5)
        outside = Path(self._tmp) / 'elsewhere.log'
        outside.write_text('secret\n', encoding='utf-8', newline='')
        self._write_transcript('redir02', 'Sneaky', command=f'bash job.sh > {outside} 2>&1')

        self.assertNotEqual(read_task_output(_SESSION, _CWD, 'redir02'), 'secret\n')

    def test_capture_file_wins_when_nonempty(self) -> None:
        # If the task's own capture file has content, it is used - the redirect
        # target is not consulted.
        self._write('redir03.output', 'captured\n', age_seconds=5)
        scratch = Path(self._tmp) / 'claude' / cwd_to_slug(_CWD) / _SESSION / 'scratchpad'
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / 'other.log').write_text('redirected\n', encoding='utf-8', newline='')
        self._write_transcript('redir03', 'Both', command=f'bash job.sh > {scratch / "other.log"} 2>&1')

        self.assertEqual(read_task_output(_SESSION, _CWD, 'redir03'), 'captured\n')


class RedirectParsingTest(unittest.TestCase):
    def test_wsl_path_translation(self) -> None:
        self.assertEqual(_wsl_to_windows('/mnt/c/Users/jens/x.log'), r'C:\Users\jens\x.log')
        self.assertEqual(_wsl_to_windows('/mnt/d/build/out'), r'D:\build\out')
        self.assertEqual(_wsl_to_windows(r'C:\already\windows'), r'C:\already\windows')

    def test_parse_redirect_target(self) -> None:
        self.assertEqual(_parse_redirect_target('bash x.sh > out.log 2>&1'), 'out.log')
        self.assertEqual(_parse_redirect_target('cmd >> "a b.log"'), 'a b.log')
        self.assertEqual(_parse_redirect_target('cmd &> both.log'), 'both.log')
        # stderr-only redirect is not the stdout target
        self.assertIsNone(_parse_redirect_target('cmd 2> err.log'))
        # a bare fd-dup is not a file
        self.assertIsNone(_parse_redirect_target('cmd 2>&1'))
        self.assertIsNone(_parse_redirect_target('plain command with no redirect'))


if __name__ == '__main__':
    unittest.main()
