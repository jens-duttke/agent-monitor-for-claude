"""Tests for the token price loader (defensive parsing, comment stripping)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_monitor_for_claude import pricing


class PricingTest(unittest.TestCase):
    def test_loads_real_pricing_without_comment(self) -> None:
        data = pricing.load_pricing()

        self.assertIn('1970-01-01', data)
        self.assertNotIn('_comment', data)

        opus = data['1970-01-01']['opus-4-8']
        for field in ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h'):
            self.assertIn(field, opus)

    def test_missing_file_yields_empty(self) -> None:
        original = pricing.PRICING_PATH
        try:
            pricing.PRICING_PATH = original.parent / 'does-not-exist.json'
            self.assertEqual(pricing.load_pricing(), {})
        finally:
            pricing.PRICING_PATH = original

    def test_malformed_file_yields_empty(self) -> None:
        original = pricing.PRICING_PATH
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'pricing.json'
            bad.write_text('{ not valid json', encoding='utf-8')
            try:
                pricing.PRICING_PATH = bad
                self.assertEqual(pricing.load_pricing(), {})
            finally:
                pricing.PRICING_PATH = original


if __name__ == '__main__':
    unittest.main()
