"""
Internationalization
=====================

Loads translations for the configured or detected system language with
English fallback.  The locale directory structure is the configuration - adding
a ``<code>.json`` file registers a language, no code change required; its
``language_name`` key is the name the settings dialog lists it under.

``T`` is one dict for the life of the process: :func:`reload_translations`
refills it in place after the language setting changed, so every module that
imported it reads the new language.
"""
from __future__ import annotations

import json
import locale
from pathlib import Path
from typing import Any

__all__ = ['LOCALE_DIR', 'available_languages', 'detect_lang_code', 'load_translations', 'reload_translations', 'T']

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
    from .settings import current_settings

    lang = locale.getlocale()[0] or ''
    for code in (current_settings().language, detect_lang_code(lang), 'en'):
        if not code:
            continue
        translations = _read_locale(code)
        if translations is not None:
            return translations

    return {}


def reload_translations() -> None:
    """Refill :data:`T` in place for the language now configured."""
    translations = load_translations()
    T.clear()
    T.update(translations)


def available_languages() -> list[dict[str, str]]:
    """Return one ``{code, name}`` per locale file, sorted by code.

    The name is the file's own ``language_name`` - the language in itself, so a
    reader finds their language whatever the interface currently shows - or
    the code when the file names none.  A malformed file is left out.
    """
    languages: list[dict[str, str]] = []
    for path in sorted(LOCALE_DIR.glob('*.json')):
        translations = _read_locale(path.stem)
        if translations is None:
            continue

        name = translations.get('language_name')
        languages.append({'code': path.stem, 'name': name if isinstance(name, str) and name else path.stem})

    return languages


T: dict[str, Any] = load_translations()
