"""
Plugin server tests
===================

The programs the installed Claude Code plugins start as language or MCP
servers, read from their manifests.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import plugin_servers
from agent_monitor_for_claude.paths import SessionRoot


class ServerCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = SessionRoot(origin='local', label=None, config_dir=self.base, proc_dir=None, claude_temp_dir=self.base)
        plugin_servers._cache.clear()
        self.addCleanup(plugin_servers._cache.clear)

    def _write(self, relative: str, value: object) -> None:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_the_commands_of_installed_plugins_servers_are_read(self) -> None:
        market = self.base / 'plugins' / 'marketplaces' / 'official'
        self._write('plugins/known_marketplaces.json', {'official': {'installLocation': str(market)}})
        self._write('plugins/installed_plugins.json', {'version': 2, 'plugins': {
            'rust-lsp@official': [{'installPath': str(self.base / 'cache' / 'rust')}],
            'tools@official': [{'installPath': str(self.base / 'cache' / 'tools')}],
        }})
        self._write('plugins/marketplaces/official/plugins/rust-lsp/.claude-plugin/plugin.json',
                    {'lspServers': {'rust-analyzer': {'command': 'rust-analyzer'}}})
        self._write('cache/tools/.claude-plugin/plugin.json', {'mcpServers': {'db': {'command': 'C:/tools/db-server.exe', 'args': ['--stdio']}}})
        # A plugin that is not installed contributes nothing.
        self._write('plugins/marketplaces/official/plugins/other/.claude-plugin/plugin.json', {'lspServers': {'x': {'command': 'gopls'}}})

        self.assertEqual(plugin_servers.server_commands(self.root), frozenset({'rust-analyzer', 'db-server'}))

    def test_no_or_broken_plugin_files_give_no_commands(self) -> None:
        self.assertEqual(plugin_servers.server_commands(self.root), frozenset())
        self._write('plugins/installed_plugins.json', {'plugins': 'nonsense'})
        plugin_servers._cache.clear()
        self.assertEqual(plugin_servers.server_commands(self.root), frozenset())

    def test_the_answer_is_kept_for_a_while(self) -> None:
        with mock.patch.object(plugin_servers, '_read_commands', return_value=frozenset({'a'})) as read:
            plugin_servers.server_commands(self.root)
            plugin_servers.server_commands(self.root)
        self.assertEqual(read.call_count, 1)


if __name__ == '__main__':
    unittest.main()
