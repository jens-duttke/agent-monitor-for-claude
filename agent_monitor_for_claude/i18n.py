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

# locale.normalize rewrites many Windows legacy 'Language_Country' names to an
# ISO code, but leaves several untouched for which the app ships a locale file.
# Map those language words by hand, keyed on the lowercased language part. A
# value that already carries a region (e.g. 'zh-CN') resolves the file directly.
_LEGACY_LANG_CODES = {
    'ukrainian': 'uk',
    'hindi': 'hi',
    'indonesian': 'id',
    'chinese (simplified)': 'zh-CN',
    'chinese (traditional)': 'zh-TW',
}

# For the unqualified 'Chinese_<Country>' form the country decides the script,
# and locale.normalize leaves the country word untranslated - so map it too.
_LEGACY_CHINESE_REGIONS = {
    'china': 'zh-CN',
    'singapore': 'zh-CN',
    'taiwan': 'zh-TW',
    'hong kong': 'zh-TW',
    'macao': 'zh-TW',
    'macau': 'zh-TW',
}


def detect_lang_code(lang: str) -> str:
    """Detect the locale file code from a system locale string.

    Lookup chain: a hand-maintained alias for the legacy Windows names
    ``locale.normalize`` misses, then ``{lang}-{REGION}.json`` ->
    ``{lang}.json`` -> ``en.json``.  Beyond those few aliases the locale
    directory *is* the configuration.

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
    region_word = parts[1].lower() if len(parts) > 1 else ''

    # On Windows, os.getlocale() returns e.g. 'German_Germany'. locale.normalize
    # rewrites many such names to an ISO code, but misses some the app ships a
    # locale for - resolve those from the alias tables first.
    alias = _LEGACY_LANG_CODES.get(base)
    if alias is None and base == 'chinese':
        alias = _LEGACY_CHINESE_REGIONS.get(region_word)
    if alias and (LOCALE_DIR / f'{alias}.json').exists():
        return alias

    # A still-descriptive base (locale.normalize did not shorten it) - re-split.
    if len(base) > 3:
        base = locale.normalize(parts[0]).split('.')[0].split('_')[0].lower()

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
