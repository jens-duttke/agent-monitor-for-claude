"""
Settings
========

Centralizes all user-tunable constants.  Structural constants (file paths,
registry keys) remain in their respective modules.

Loads an optional ``agent-monitor-settings.json`` to let users override any
constant.  Search order:

1. Next to the executable (frozen) or project root (source)
2. ``$CLAUDE_CONFIG_DIR/agent-monitor-settings.json`` (if set and different from ``~/.claude/``)
3. ``~/.claude/agent-monitor-settings.json``

The app never creates this file - users place it manually.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    'SETTINGS_FILENAME',
    'POLL_INTERVAL', 'ENDED_MAX_AGE',
    'SUBAGENT_RECENT_SECONDS',
    'INCLUDE_COMPLETED', 'WINDOW_WIDTH', 'WINDOW_HEIGHT',
    'LANGUAGE',
]

SETTINGS_FILENAME = 'agent-monitor-settings.json'

_NUMERIC_BOUNDS: dict[str, int] = {
    'poll_interval': 1,
    'ended_max_age': 0,
    'subagent_recent_seconds': 1,
    'window_width': 320,
    'window_height': 240,
}
_BOOL_KEYS = frozenset({'include_completed'})
_STRING_KEYS = frozenset({'language'})


def _load_settings() -> dict[str, Any]:
    """Read the first ``agent-monitor-settings.json`` found, or return ``{}``."""
    if getattr(sys, 'frozen', False):
        app_dir = Path(sys.executable).parent
    else:
        app_dir = Path(__file__).resolve().parent.parent

    home_claude = Path.home() / '.claude'
    custom_config = Path(os.environ['CLAUDE_CONFIG_DIR']) if os.environ.get('CLAUDE_CONFIG_DIR') else None

    search_paths = [app_dir / SETTINGS_FILENAME]
    if custom_config and custom_config != home_claude:
        search_paths.append(custom_config / SETTINGS_FILENAME)
    search_paths.append(home_claude / SETTINGS_FILENAME)

    for path in search_paths:
        if path.is_file():
            return _read(path)

    return {}


def _read(path: Path) -> dict[str, Any]:
    """Read and validate one settings file, showing a dialog on hard errors."""
    try:
        # utf-8-sig tolerates a byte-order mark: this is the one file users hand-
        # create, and editors like Notepad may save it with a BOM that plain
        # utf-8 would leave in front of the JSON, breaking an otherwise valid file.
        text = path.read_text(encoding='utf-8-sig').strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f'Expected a JSON object, got {type(data).__name__}')
        return _validate(data, path)
    except (json.JSONDecodeError, ValueError) as exc:
        _error_dialog(f'Invalid JSON in settings file:\n{path}\n\n{exc}')
        return {}
    except OSError:
        return {}


def _validate(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Drop entries with invalid types or values and report them in a dialog."""
    errors: list[str] = []
    drop: list[str] = []

    for key, value in data.items():
        if key in _NUMERIC_BOUNDS:
            minimum = _NUMERIC_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f'  {key}: expected an integer, got {type(value).__name__}')
                drop.append(key)
            elif value < minimum:
                errors.append(f'  {key}: must be >= {minimum}, got {value}')
                drop.append(key)

        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                errors.append(f'  {key}: expected true or false, got {type(value).__name__}')
                drop.append(key)

        elif key in _STRING_KEYS:
            if not isinstance(value, str):
                errors.append(f'  {key}: expected a string, got {type(value).__name__}')
                drop.append(key)

    for key in drop:
        del data[key]

    if errors:
        _error_dialog(f'Invalid values in settings file:\n{path}\n\n' + '\n'.join(errors))

    return data


def _error_dialog(message: str) -> None:
    """Show a modal error dialog (Windows message box)."""
    ctypes.windll.user32.MessageBoxW(0, message, 'Agent Monitor for Claude - Settings Error', 0x30)


_S = _load_settings()

# Poll cadence (seconds)
POLL_INTERVAL: int = _S.get('poll_interval', 5)

# How long an ended session stays visible (seconds); also see INCLUDE_COMPLETED
ENDED_MAX_AGE: int = _S.get('ended_max_age', 3600)
INCLUDE_COMPLETED: bool = _S.get('include_completed', False)

# A subagent is running until its transcript ends with end_turn; agent files
# older than this are treated as long finished and ignored.
SUBAGENT_RECENT_SECONDS: int = _S.get('subagent_recent_seconds', 900)

# Window size (logical pixels)
WINDOW_WIDTH: int = _S.get('window_width', 920)
WINDOW_HEIGHT: int = _S.get('window_height', 680)

# Language override (empty = auto-detect from system locale)
LANGUAGE: str = _S.get('language', '')
