"""
Token Pricing
=============

Loads the hand-maintained token price list (``pricing.json`` at the repo root)
and hands it to the UI verbatim.  The file is the configuration: top-level keys
are effective-from dates and the UI picks the schedule current for today, so a
future price change can be entered ahead of time and takes effect on its own.

Prices are display data, not conversation content, and never leave the machine.
Parsing is defensive - a missing or malformed file yields an empty table, and
the UI simply shows a plain token total instead of a cost.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ['PRICING_PATH', 'load_pricing']

PRICING_PATH = Path(__file__).parent.parent / 'pricing.json'


def load_pricing() -> dict[str, Any]:
    """Return the price schedules as a plain dict, or empty on any error.

    A leading ``_comment`` key (documentation inside the JSON) is dropped so
    only real date schedules reach the UI.
    """
    try:
        data = json.loads(PRICING_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    return {key: value for key, value in data.items() if isinstance(value, dict) and not key.startswith('_')}
