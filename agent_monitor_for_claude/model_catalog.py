"""
Model Catalog
=============

The context-window size of each model, from the model catalog Claude Code caches locally.

Claude Code fetches its model catalog and keeps a copy under ``cache/model-catalog/`` in its config
directory, one JSON file per account and surface.  Each model entry carries ``runtime.max_input_tokens``,
which is exactly the size of the window a session's context fills - and the one number a fixed table
would get wrong, since it differs per model and changes with new releases.  Nothing else from those
files is taken: no account data, no notices, no settings.

The files change only when Claude Code refetches the catalog, so a root's table is re-read at most once
per ``_REFRESH_SECONDS``.  An absent, unreadable or reshaped catalog yields an empty table, and the UI
then shows a context size without a percentage.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .paths import SessionRoot, model_catalog_dir

__all__ = ['context_window']

_REFRESH_SECONDS = 60.0

# A bracketed tier on a model id, e.g. "claude-opus-5-5[1m]"; the catalog lists the base id.
_TIER_SUFFIX_PATTERN = re.compile(r'\[[^\]]*\]$')

# catalog directory -> (monotonic time read, model id -> max input tokens).  Keyed
# by the directory rather than the root's origin, so a changed config directory
# is never answered from the previous one's table.
_tables: dict[str, tuple[float, dict[str, int]]] = {}
_tables_lock = threading.Lock()


def context_window(root: SessionRoot, model_id: str | None) -> int | None:
    """Return the context-window size in tokens of *model_id* under *root*, or None when the catalog does not name it.

    Parameters
    ----------
    root : SessionRoot
        The session root whose cached catalog applies - a WSL distro keeps its own.
    model_id : str or None
        The model id as a transcript records it; a bracketed tier suffix is ignored when the exact id
        is not listed.
    """
    if not isinstance(model_id, str) or not model_id:
        return None

    table = _table(root)
    window = table.get(model_id)
    if window is None:
        window = table.get(_TIER_SUFFIX_PATTERN.sub('', model_id))

    return window


def _table(root: SessionRoot) -> dict[str, int]:
    """Return *root*'s model-id table, re-read when the cached one is older than ``_REFRESH_SECONDS``."""
    directory = model_catalog_dir(root)
    key = str(directory)
    now = time.monotonic()
    with _tables_lock:
        cached = _tables.get(key)
        if cached is not None and now - cached[0] < _REFRESH_SECONDS:
            return cached[1]

    table = _read_catalogs(directory)
    with _tables_lock:
        _tables[key] = (now, table)

    return table


def _read_catalogs(directory: Path) -> dict[str, int]:
    """Merge every catalog file in *directory* into one table, a newer file overriding an older one."""
    try:
        files = sorted((entry for entry in os.scandir(directory) if entry.name.endswith('.json') and entry.is_file()),
                       key=lambda entry: entry.stat().st_mtime)
    except OSError:
        return {}

    table: dict[str, int] = {}
    for entry in files:
        try:
            with open(entry.path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        table.update(_windows_from(data))

    return table


def _windows_from(data: object) -> dict[str, int]:
    """Return ``{model id: max input tokens}`` from one parsed catalog file, skipping anything malformed."""
    catalog = data.get('catalog') if isinstance(data, dict) else None
    config = catalog.get('config') if isinstance(catalog, dict) else None
    models = config.get('models') if isinstance(config, dict) else None
    if not isinstance(models, list):
        return {}

    windows: dict[str, int] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get('id')
        runtime = model.get('runtime')
        tokens = runtime.get('max_input_tokens') if isinstance(runtime, dict) else None
        if not isinstance(model_id, str) or not model_id:
            continue
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
            continue
        windows[model_id] = tokens

    return windows
