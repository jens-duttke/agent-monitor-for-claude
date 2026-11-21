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

    def test_a_wsl_roots_manifest_paths_are_reached_through_the_host(self) -> None:
        wsl_root = SessionRoot(origin='wsl:Ubuntu', label='Ubuntu', config_dir=self.base, proc_dir=self.base, claude_temp_dir=self.base)
        self._write('plugins/known_marketplaces.json', {'official': {'installLocation': '/home/me/.claude/plugins/marketplaces/official'}})
        self._write('plugins/installed_plugins.json', {'plugins': {
            'pyright-lsp@official': [{'installPath': '/home/me/.claude/plugins/cache/pyright'}],
            'ts-lsp@official': [{'installPath': '/home/me/.claude/plugins/cache/ts'}],
        }})
        self._write('plugins/marketplaces/official/plugins/pyright-lsp/.claude-plugin/plugin.json',
                    {'lspServers': {'pyright': {'command': 'pyright-langserver'}}})
        self._write('plugins/cache/ts/.claude-plugin/plugin.json', {'lspServers': {'ts': {'command': 'typescript-language-server'}}})

        def host_path(root: SessionRoot, path_text: str) -> str:
            self.assertIs(root, wsl_root)
            return str(self.base / path_text.removeprefix('/home/me/.claude/'))

        with mock.patch.object(plugin_servers, 'host_path', side_effect=host_path):
            commands = plugin_servers.server_commands(wsl_root)
        self.assertEqual(commands, frozenset({'pyright-langserver', 'typescript-language-server'}))

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
