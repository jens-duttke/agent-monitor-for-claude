"""
Settings
========

The user-tunable settings: one schema (:data:`SPECS`) describes every
setting - its type, bounds, the group the settings dialog lists it under and
when a change takes effect - and drives the validation of the settings file,
the defaults and the dialog alike.  Structural constants (file paths, registry
keys) remain in their respective modules.

The values live in ``agent-monitor-settings.json``.  The file read is the
first one found of:

1. Next to the executable (frozen) or in the project root (source) - a portable
   setup, which is also where a change is saved back to
2. The user's configuration directory (``platforms.user_config_dir``) - where
   a change is saved otherwise
3. ``$CLAUDE_CONFIG_DIR/agent-monitor-settings.json`` (if set and different
   from ``~/.claude/``), then ``~/.claude/agent-monitor-settings.json`` -
   earlier locations, read but never written: saving moves the values to (2)

The settings dialog writes the file through :func:`save_settings`, the only
write here: atomically (a temporary file beside it, then a replace), keeping
only values that differ from their default plus any ``_``-prefixed comment
key.  Saving replaces the immutable :class:`Settings` the rest of the package
reads through :func:`current_settings`, so a change reaches every reader on
its next call without a restart.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Collection
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .platforms import IS_WINDOWS, show_error_box, user_config_dir

__all__ = [
    'SETTINGS_FILENAME', 'SPECS', 'SettingSpec', 'Settings',
    'current_settings', 'describe_settings', 'save_settings', 'settings_path',
]

SETTINGS_FILENAME = 'agent-monitor-settings.json'

# A language code as the locale files are named (``de``, ``pt-BR``, ``zh-CN``).
# Which codes exist is checked on save, against the locale files themselves.
_LANGUAGE_PATTERN = re.compile(r'^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,4})?$')


@dataclass(frozen=True)
class Settings:
    """The settings in force, with their defaults; field names are the file's keys."""

    poll_interval: int = 5
    ended_max_age: int = 3600
    include_completed: bool = False
    subagent_recent_seconds: int = 900
    window_width: int = 1180
    window_height: int = 760
    language: str = ''
    wsl: bool = True


@dataclass(frozen=True)
class SettingSpec:
    """How one setting is typed, bounded, grouped and applied.

    ``kind`` is ``int``, ``bool`` or ``language``; ``applies`` is ``now`` (on
    the next read), ``reload`` (the page reloads, for the language) or
    ``restart`` (the next start, for the initial window size).
    """

    key: str
    kind: str
    group: str
    minimum: int | None = None
    maximum: int | None = None
    unit: str = ''
    applies: str = 'now'
    windows_only: bool = False


# In the order the settings dialog lists them, grouped by ``group``.
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec('language', 'language', 'general', applies='reload'),
    SettingSpec('poll_interval', 'int', 'refresh', minimum=1, maximum=3600, unit='seconds'),
    SettingSpec('ended_max_age', 'int', 'sessions', minimum=0, unit='seconds'),
    SettingSpec('include_completed', 'bool', 'sessions'),
    SettingSpec('subagent_recent_seconds', 'int', 'sessions', minimum=1, unit='seconds'),
    SettingSpec('wsl', 'bool', 'wsl', windows_only=True),
    SettingSpec('window_width', 'int', 'window', minimum=320, maximum=10000, unit='pixels', applies='restart'),
    SettingSpec('window_height', 'int', 'window', minimum=240, maximum=10000, unit='pixels', applies='restart'),
)

_SPECS_BY_KEY = {spec.key: spec for spec in SPECS}

# Where the values were read from: next to the executable, the user's
# configuration directory, an earlier location, or no file at all.
_SOURCE_PORTABLE = 'portable'
_SOURCE_CONFIG = 'config'
_SOURCE_LEGACY = 'legacy'
_SOURCE_NONE = 'none'


@dataclass(frozen=True)
class _Loaded:
    """What one read of the settings file produced."""

    settings: Settings
    path: Path | None
    source: str
    errors: tuple[dict[str, Any], ...]
    comments: dict[str, Any]


class _Store:
    """The settings in force; replaced as a whole on save, guarded for writers by ``lock``."""

    def __init__(self, loaded: _Loaded) -> None:
        self.loaded = loaded
        self.lock = threading.Lock()


def current_settings() -> Settings:
    """Return the settings in force.

    The returned object is immutable and replaced as a whole on save, so a
    caller reading several fields from one call sees one consistent set.
    """
    return _store.loaded.settings


def settings_path() -> Path:
    """Return the file the next save writes: next to the executable when one exists there, else the configuration directory."""
    portable = _portable_path()
    if portable.is_file():
        return portable

    return user_config_dir() / SETTINGS_FILENAME


def describe_settings() -> dict[str, Any]:
    """Describe the settings for the settings dialog.

    Returns
    -------
    dict
        ``specs`` (the settings that apply on this system, in dialog order),
        ``values`` (in force), ``defaults``, ``errors`` (what the last read of
        the file rejected), ``path`` (the file a save writes), ``exists``
        (whether that file exists yet), ``source`` (where the values were read
        from) and ``legacy_path`` (the earlier location read, or ``''``).
    """
    loaded = _store.loaded
    target = settings_path()
    specs = [_spec_record(spec) for spec in SPECS if _applies_here(spec)]
    legacy = str(loaded.path) if loaded.source == _SOURCE_LEGACY and loaded.path else ''

    return {
        'specs': specs,
        'values': asdict(loaded.settings),
        'defaults': asdict(Settings()),
        'errors': [dict(error) for error in loaded.errors],
        'path': str(target),
        'exists': target.is_file(),
        'source': loaded.source,
        'legacy_path': legacy,
    }


def save_settings(values: object, known_languages: Collection[str]) -> dict[str, Any]:
    """Validate *values*, write them to the settings file and put them in force.

    Parameters
    ----------
    values
        A mapping of setting keys to new values, as the settings dialog sends
        it; keys left out keep the value in force.
    known_languages
        The language codes a locale file exists for.  An empty language means
        automatic detection and is always accepted.

    Returns
    -------
    dict
        ``ok``; on failure ``errors`` (one record per rejected key, or one with
        an empty key for a write failure); on success ``values`` (in force),
        ``changed`` (the keys whose value changed), ``restart`` (the changed
        keys that wait for the next start), ``reload`` (whether the page has
        to reload, for a new language) and ``path`` (the file written).
    """
    if not isinstance(values, dict):
        return {'ok': False, 'errors': [{'key': '', 'code': 'invalid'}]}

    with _store.lock:
        loaded = _store.loaded
        valid, errors = _validate(values)
        language = valid.get('language')
        if isinstance(language, str) and language and language not in known_languages:
            errors.append({'key': 'language', 'code': 'language'})
        if errors:
            return {'ok': False, 'errors': errors}

        merged = asdict(loaded.settings)
        merged.update(valid)
        updated = Settings(**merged)

        target = settings_path()
        comments = _comments_for(target, loaded)
        failure = _write(target, _file_content(updated, comments))
        if failure is not None:
            return {'ok': False, 'errors': [failure]}

        source = _SOURCE_PORTABLE if target == _portable_path() else _SOURCE_CONFIG
        _store.loaded = _Loaded(updated, target, source, (), comments)

    changed = [spec.key for spec in SPECS if getattr(updated, spec.key) != getattr(loaded.settings, spec.key)]

    return {
        'ok': True,
        'values': asdict(updated),
        'changed': changed,
        'restart': [key for key in changed if _SPECS_BY_KEY[key].applies == 'restart'],
        'reload': any(_SPECS_BY_KEY[key].applies == 'reload' for key in changed),
        'path': str(target),
    }


def _applies_here(spec: SettingSpec) -> bool:
    """Whether *spec* means anything on this system (WSL is a Windows feature)."""
    return IS_WINDOWS or not spec.windows_only


def _spec_record(spec: SettingSpec) -> dict[str, Any]:
    """One spec as plain values for the bridge."""
    return {
        'key': spec.key, 'kind': spec.kind, 'group': spec.group, 'minimum': spec.minimum, 'maximum': spec.maximum,
        'unit': spec.unit, 'applies': spec.applies,
    }


def _app_dir() -> Path:
    """The directory of the executable (frozen) or the project root (source)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent

    return Path(__file__).resolve().parent.parent


def _portable_path() -> Path:
    """The settings file next to the executable."""
    return _app_dir() / SETTINGS_FILENAME


def _candidates() -> list[tuple[str, Path]]:
    """The settings files to look for, in reading order, each with its source."""
    home_claude = Path.home() / '.claude'
    custom_config = Path(os.environ['CLAUDE_CONFIG_DIR']) if os.environ.get('CLAUDE_CONFIG_DIR') else None

    candidates = [(_SOURCE_PORTABLE, _portable_path()), (_SOURCE_CONFIG, user_config_dir() / SETTINGS_FILENAME)]
    if custom_config and custom_config != home_claude:
        candidates.append((_SOURCE_LEGACY, custom_config / SETTINGS_FILENAME))
    candidates.append((_SOURCE_LEGACY, home_claude / SETTINGS_FILENAME))

    return candidates


def _load() -> _Loaded:
    """Read the first settings file found; no file yields the defaults."""
    for source, path in _candidates():
        if path.is_file():
            data, errors = _read(path)
            valid, value_errors = _validate({key: value for key, value in data.items() if not key.startswith('_')})
            comments = {key: value for key, value in data.items() if key.startswith('_')}
            merged = asdict(Settings())
            merged.update(valid)

            return _Loaded(Settings(**merged), path, source, tuple(errors + value_errors), comments)

    return _Loaded(Settings(), None, _SOURCE_NONE, (), {})


def _read(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read one settings file as a JSON object, with an error record when it is not one."""
    try:
        # utf-8-sig tolerates a byte-order mark: an editor like Notepad may save
        # a hand-edited file with one, which plain utf-8 would leave in front of
        # the JSON and break an otherwise valid file.
        text = path.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [{'key': '', 'code': 'unreadable', 'detail': str(exc)}]

    if not text:
        return {}, []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [{'key': '', 'code': 'invalid_json', 'detail': str(exc)}]

    if not isinstance(data, dict):
        return {}, [{'key': '', 'code': 'not_object', 'detail': type(data).__name__}]

    return data, []


def _validate(data: dict[Any, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split *data* into the valid values and one error record per rejected key.

    An unknown key is reported like a type error: it is almost always a typo
    (``pollinterval``), and silently applying the default would hide it.
    """
    valid: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    for key, value in data.items():
        spec = _SPECS_BY_KEY.get(key) if isinstance(key, str) else None
        if spec is None:
            errors.append({'key': str(key), 'code': 'unknown'})
            continue

        error = _check(spec, value)
        if error is not None:
            errors.append(error)
            continue

        valid[key] = value

    return valid, errors


def _check(spec: SettingSpec, value: object) -> dict[str, Any] | None:
    """The error record for *value* under *spec*, or None when it is valid."""
    if spec.kind == 'bool':
        return None if isinstance(value, bool) else {'key': spec.key, 'code': 'type', 'expected': 'boolean'}

    if spec.kind == 'language':
        if not isinstance(value, str):
            return {'key': spec.key, 'code': 'type', 'expected': 'string'}
        return None if not value or _LANGUAGE_PATTERN.match(value) else {'key': spec.key, 'code': 'language'}

    if isinstance(value, bool) or not isinstance(value, int):
        return {'key': spec.key, 'code': 'type', 'expected': 'integer'}
    if spec.minimum is not None and value < spec.minimum:
        return {'key': spec.key, 'code': 'minimum', 'limit': spec.minimum}
    if spec.maximum is not None and value > spec.maximum:
        return {'key': spec.key, 'code': 'maximum', 'limit': spec.maximum}

    return None


def _comments_for(target: Path, loaded: _Loaded) -> dict[str, Any]:
    """The ``_``-prefixed keys the written file keeps.

    Read afresh from *target* when it exists, so a comment added by hand while
    the app runs survives a save; a save that moves the values to a new file
    carries over the comments of the file they were read from.
    """
    if not target.is_file():
        return dict(loaded.comments)

    data, _ = _read(target)

    return {key: value for key, value in data.items() if key.startswith('_')}


def _file_content(values: Settings, comments: dict[str, Any]) -> str:
    """The file text for *values*: the comments, then every value that differs from its default."""
    defaults = Settings()
    content: dict[str, Any] = dict(comments)
    for field in fields(Settings):
        value = getattr(values, field.name)
        if value != getattr(defaults, field.name):
            content[field.name] = value

    return json.dumps(content, indent=4, ensure_ascii=False) + '\n'


def _write(target: Path, text: str) -> dict[str, Any] | None:
    """Write *text* to *target* atomically; return an error record on failure."""
    temporary = target.with_name(target.name + '.tmp')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return {'key': '', 'code': 'write', 'detail': exc.strerror or str(exc)}

    return None


def _error_text(error: dict[str, Any]) -> str:
    """One error record as a line of the start-up dialog (English, like every platform dialog)."""
    key = error.get('key') or ''
    code = error.get('code')
    if code == 'unknown':
        return f'  {key}: unknown setting'
    if code == 'type':
        return f'  {key}: expected {error.get("expected")}'
    if code == 'minimum':
        return f'  {key}: must be >= {error.get("limit")}'
    if code == 'maximum':
        return f'  {key}: must be <= {error.get("limit")}'
    if code == 'language':
        return f'  {key}: not a language code'

    return f'  {error.get("detail", code)}'


def _report(loaded: _Loaded) -> None:
    """Show the errors of the read at start in a dialog naming the file."""
    if not loaded.errors:
        return

    lines = '\n'.join(_error_text(error) for error in loaded.errors)
    show_error_box(f'Invalid settings file:\n{loaded.path}\n\n{lines}\n\nInvalid entries use their default.', 'Agent Monitor for Claude - Settings Error')


_store = _Store(_load())
_report(_store.loaded)
