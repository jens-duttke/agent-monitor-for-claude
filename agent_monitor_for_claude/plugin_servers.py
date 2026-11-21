"""
Plugin Servers
==============

The programs the installed Claude Code plugins start as language servers
(``lspServers``) and MCP servers (``mcpServers``), so a background process
running one can be told apart from a tool the session runs: Claude Code starts
such a server on first use and keeps it for the rest of the session, which the
start-time window of ``procinfo.SESSION_HELPER_WINDOW_SECONDS`` cannot see.

Only the plugin manifests are read - ``installed_plugins.json``,
``known_marketplaces.json``, each installed plugin's ``plugin.json`` and its
marketplace's ``marketplace.json`` - and of them only each server's
``command``.  ``~/.claude.json`` is never read: it holds account data.  The
answer is kept for ``_REFRESH_SECONDS`` per root.  Every read degrades: a
missing or malformed file contributes nothing.
"""

from __future__ import annotations

import json
import ntpath
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .paths import SessionRoot, host_path, plugins_dir

__all__ = ['server_commands']

# How long one root's answer is kept before the manifests are read again.
_REFRESH_SECONDS = 60.0
# The server lists a plugin manifest may carry.
_SERVER_KEYS = ('lspServers', 'mcpServers')

# root origin -> (monotonic time read, commands)
_cache: dict[str, tuple[float, frozenset[str]]] = {}
_lock = threading.Lock()


def server_commands(root: SessionRoot) -> frozenset[str]:
    """Return the lowercased program names (no directory, no extension) of every server the root's installed plugins configure."""
    now = time.monotonic()
    with _lock:
        cached = _cache.get(root.origin)
        if cached is not None and now - cached[0] < _REFRESH_SECONDS:
            return cached[1]

    commands = _read_commands(plugins_dir(root), lambda path_text: Path(host_path(root, path_text)))
    with _lock:
        _cache[root.origin] = (now, commands)

    return commands


def _read_commands(plugins: Path, locate: Callable[[str], Path]) -> frozenset[str]:
    """Read the server commands of every installed plugin below *plugins*.

    The manifests name install locations as the root's own system writes
    them - a WSL distro's as POSIX paths - so each goes through *locate* to
    become a path this host can open.
    """
    installed = _load(plugins / 'installed_plugins.json')
    marketplaces = _load(plugins / 'known_marketplaces.json')
    entries = installed.get('plugins') if isinstance(installed, dict) else None
    if not isinstance(entries, dict):
        return frozenset()

    commands: set[str] = set()
    for key, installs in entries.items():
        if not isinstance(key, str):
            continue
        name, _, marketplace = key.partition('@')
        market_location = _install_location(marketplaces, marketplace, locate)
        manifests = [_market_entry(market_location, name)]
        if market_location is not None:
            manifests.append(_load(market_location / 'plugins' / name / '.claude-plugin' / 'plugin.json'))
        for install in installs if isinstance(installs, list) else ():
            path = install.get('installPath') if isinstance(install, dict) else None
            if isinstance(path, str) and path:
                manifests.append(_load(locate(path) / '.claude-plugin' / 'plugin.json'))
        for manifest in manifests:
            commands.update(_manifest_commands(manifest))

    return frozenset(commands)


def _manifest_commands(manifest: Any) -> set[str]:
    """The program names of a manifest's servers."""
    found: set[str] = set()
    if not isinstance(manifest, dict):
        return found
    for key in _SERVER_KEYS:
        servers = manifest.get(key)
        for server in servers.values() if isinstance(servers, dict) else ():
            command = server.get('command') if isinstance(server, dict) else None
            if isinstance(command, str) and command.strip():
                found.add(ntpath.splitext(ntpath.basename(command.strip()))[0].lower())

    return found


def _install_location(marketplaces: Any, marketplace: str, locate: Callable[[str], Path]) -> Path | None:
    """Where *marketplace* is installed, or None when the index does not say."""
    entry = marketplaces.get(marketplace) if isinstance(marketplaces, dict) else None
    location = entry.get('installLocation') if isinstance(entry, dict) else None

    return locate(location) if isinstance(location, str) and location else None


def _market_entry(location: Path | None, name: str) -> Any:
    """Plugin *name*'s entry in the ``marketplace.json`` at *location*, which may carry its servers inline."""
    if location is None:
        return None
    index = _load(location / '.claude-plugin' / 'marketplace.json')
    plugins = index.get('plugins') if isinstance(index, dict) else None
    for plugin in plugins if isinstance(plugins, list) else ():
        if isinstance(plugin, dict) and plugin.get('name') == name:
            return plugin

    return None


def _load(path: Path) -> Any:
    """Parse one JSON file, or None when it cannot be read or parsed."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError):
        return None
