"""
Test results
============

Recognize the summary a test runner prints at the end of its output - pytest,
unittest, Jest, Vitest, Node's built-in runner (and TAP in general), cargo and
go test - and reduce the newest one to a few numbers.  Pure text processing:
the caller reads the output, this module never touches a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ['TestSummary', 'test_summary']

# Colour and cursor sequences a runner writes around its summary.
_ANSI_PATTERN = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_COUNT_PATTERN = re.compile(r'(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)')

_PYTEST_PATTERN = re.compile(r'^=+ (?P<body>.*\b(?:passed|failed|errors?|skipped|no tests ran)\b.*?) in [\d.]+s(?: \([^)]*\))? =+$')
_UNITTEST_RAN_PATTERN = re.compile(r'^Ran (?P<total>\d+) tests? in [\d.]+s$')
_UNITTEST_RESULT_PATTERN = re.compile(r'^(?P<verdict>OK|FAILED)(?: \((?P<body>[^)]*)\))?$')
_JEST_PATTERN = re.compile(r'^Tests:\s+(?P<body>.*?\d+ total)$')
_VITEST_PATTERN = re.compile(r'^Tests\s+(?P<body>(?:\d+ \w+(?: \| )?)+)\s*\((?P<total>\d+)\)$')
_TAP_PATTERN = re.compile(r'^(?:ℹ|#) (?P<kind>tests|pass|fail|skipped|skip|todo) (?P<count>\d+)$')
_CARGO_PATTERN = re.compile(r'^test result: (?P<verdict>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; (?P<ignored>\d+) ignored')
# Go's package line is tab-separated - ``ok  \t<package>\t0.1s`` - which keeps
# an ordinary line opening with "ok" from counting.
_GO_PACKAGE_PATTERN = re.compile(r'^(?P<verdict>ok|FAIL) *\t\S+\t')


@dataclass(frozen=True)
class TestSummary:
    """The newest test run's outcome: the runner, its counts and whether it failed."""

    __test__ = False

    runner: str
    passed: int
    failed: int
    skipped: int

    @property
    def ok(self) -> bool:
        """Whether nothing failed."""
        return self.failed == 0


def test_summary(text: str) -> TestSummary | None:
    """Return the summary of the newest test run in *text*, or None when it holds none.

    The output is read from its end, so a run repeated in watch mode reports
    its latest result.  Go prints one line per package; those are counted as a
    whole, since a package line is the only summary it writes.
    """
    if not isinstance(text, str) or not text:
        return None

    lines = [_ANSI_PATTERN.sub('', line).strip() for line in text.splitlines()]
    tap: dict[str, int] = {}
    go_ok = go_failed = 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        previous = next((lines[earlier] for earlier in range(index - 1, -1, -1) if lines[earlier]), '')
        summary = _line_summary(line, previous)
        if summary is not None:
            return summary

        tap_match = _TAP_PATTERN.match(line)
        if tap_match:
            tap.setdefault(tap_match.group('kind'), int(tap_match.group('count')))
            if 'pass' in tap and 'fail' in tap:
                return TestSummary('tap', tap['pass'], tap['fail'], tap.get('skipped', tap.get('skip', 0)))
            continue

        go_match = _GO_PACKAGE_PATTERN.match(line)
        if go_match:
            if go_match.group('verdict') == 'ok':
                go_ok += 1
            else:
                go_failed += 1

    if go_ok or go_failed:
        return TestSummary('go', go_ok, go_failed, 0)

    return None


def _line_summary(line: str, previous: str) -> TestSummary | None:
    """The summary *line* states on its own (with the non-empty line before it, for unittest), or None."""
    match = _PYTEST_PATTERN.match(line)
    if match:
        counts = _counts(match.group('body'))
        passed = counts.get('passed', 0) + counts.get('xpassed', 0)
        failed = counts.get('failed', 0) + counts.get('error', 0) + counts.get('errors', 0)
        return TestSummary('pytest', passed, failed, counts.get('skipped', 0) + counts.get('xfailed', 0))

    match = _UNITTEST_RESULT_PATTERN.match(line)
    ran = _UNITTEST_RAN_PATTERN.match(previous)
    if match and ran:
        total = int(ran.group('total'))
        details = dict(part.split('=', 1) for part in (match.group('body') or '').split(', ') if '=' in part)
        failed = sum(int(details.get(key, '0')) for key in ('failures', 'errors') if details.get(key, '0').isdigit())
        skipped = int(details['skipped']) if details.get('skipped', '').isdigit() else 0
        return TestSummary('unittest', max(0, total - failed - skipped), failed, skipped)

    match = _JEST_PATTERN.match(line)
    if match:
        counts = _counts(match.group('body').replace(',', ''))
        return TestSummary('jest', counts.get('passed', 0), counts.get('failed', 0), counts.get('skipped', 0))

    match = _VITEST_PATTERN.match(line)
    if match:
        counts = _counts(match.group('body').replace('|', ''))
        return TestSummary('vitest', counts.get('passed', 0), counts.get('failed', 0), counts.get('skipped', 0))

    match = _CARGO_PATTERN.match(line)
    if match:
        return TestSummary('cargo', int(match.group('passed')), int(match.group('failed')), int(match.group('ignored')))

    return None


def _counts(body: str) -> dict[str, int]:
    """Map each ``<number> <word>`` pair in *body* to its number."""
    return {word: int(number) for number, word in _COUNT_PATTERN.findall(body)}
