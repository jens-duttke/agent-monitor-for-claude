"""
Internationalization
=====================

Loads translations for the detected system language with English fallback.
The locale directory structure is the configuration - adding a
``<code>.json`` file registers a language, no code change required.
"""
from __future__ import annotations

import json
import locale
from pathlib import Path
from typing import Any

__all__ = ['LOCALE_DIR', 'detect_lang_code', 'load_translations', 'T']

LOCALE_DIR = Path(__file__).parent.parent / 'locale'


def detect_lang_code(lang: str) -> str:
    """Detect the locale file code from a system locale string.

    Lookup chain: ``{lang}-{REGION}.json`` -> ``{lang}.json`` -> ``en.json``.
    No mapping table required - the locale directory *is* the configuration.

    Parameters
    ----------
    lang : str
        System locale string, e.g. ``'de_DE'`` or ``'German_Germany'``.

    Returns
    -------
    str
        Locale file code (without ``.json``).
    """
    normalized = locale.normalize(lang).split('.')[0]
    parts = normalized.split('_', 1)
    base = parts[0].lower()

    # On Windows, os.getlocale() returns e.g. 'German_Germany', and locale.normalize()
    # fails to rewrite it to an ISO code, so base becomes 'german'. Re-split to match.
    if len(base) > 3:
        base = locale.normalize(parts[0]).split('.')[0].split('_')[0].lower()

    if base == 'ukrainian':
        base = 'uk'

    region = parts[1] if len(parts) > 1 and len(base) <= 3 else ''

    if region and (LOCALE_DIR / f'{base}-{region}.json').exists():
        return f'{base}-{region}'
    if (LOCALE_DIR / f'{base}.json').exists():
        return base

    return 'en'


def _read_locale(code: str) -> dict[str, Any] | None:
    """Read one locale file by code, or None if it is missing or malformed."""
    try:
        data = json.loads((LOCALE_DIR / f'{code}.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def load_translations() -> dict[str, Any]:
    """Load translations for the configured or detected language, English fallback.

    Tries the configured override, then the detected locale, then English, and
    finally an empty table - so a missing or malformed locale file degrades to
    the next candidate instead of crashing at startup (the UI falls back to its
    built-in default labels for any key an empty table omits).
    """
    from .settings import LANGUAGE

    lang = locale.getlocale()[0] or ''
    for code in (LANGUAGE, detect_lang_code(lang), 'en'):
        if not code:
            continue
        translations = _read_locale(code)
        if translations is not None:
            return translations

    return {}


T: dict[str, Any] = load_translations()
