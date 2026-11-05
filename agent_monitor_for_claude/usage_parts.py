"""
Usage Parts
===========

Adds up a session's token usage from the files it is spread over and keeps the
parts apart that the cost tooltip names.  A session's spend lives in its own
transcript *and* in one transcript per subagent (``subagents/agent-*.jsonl``),
so the session total is their sum; the parts say how much of it went to the
main conversation, to each kind of subagent, and to each skill a turn ran
under, plus how many web searches were made.

Pure functions over plain dicts - no file access - so the arithmetic is tested
on its own and the readers in :mod:`transcript` and :mod:`subagents` stay about
reading.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ['UsageScan', 'add_model_usage', 'merge_scans', 'session_usage']

# {model id: {usage key: tokens}} - the shape every per-model usage takes here.
ModelUsage = dict[str, dict[str, int]]


@dataclass(frozen=True)
class UsageScan:
    """The usage one transcript file holds: per model, per skill and model, and its web searches.

    The same scan also says when the file's first entry was written, how many
    tool calls its replies made and how many lines its sidechain edits changed -
    for a subagent's own file, what the agent did.
    """

    by_model: ModelUsage = field(default_factory=dict)
    by_skill: dict[str, ModelUsage] = field(default_factory=dict)
    web_searches: int = 0
    started_at: float | None = None
    tool_calls: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    # Each tool its replies called: how often, when last (POSIX seconds), and how often it failed.
    tools: dict[str, tuple[int, float | None, int]] = field(default_factory=dict)


def add_model_usage(target: ModelUsage, source: Mapping[str, Mapping[str, int]]) -> None:
    """Add every model's token counts in *source* to *target*, creating models and keys as needed."""
    for model, usage in source.items():
        bucket = target.setdefault(model, {})
        for key, value in usage.items():
            bucket[key] = bucket.get(key, 0) + value


def merge_scans(scans: Iterable[UsageScan]) -> UsageScan:
    """Return one scan holding the sum of *scans*."""
    by_model: ModelUsage = {}
    by_skill: dict[str, ModelUsage] = {}
    web_searches = tool_calls = lines_added = lines_removed = 0
    started: list[float] = []
    tools: dict[str, tuple[int, float | None, int]] = {}
    for scan in scans:
        for name, (calls, last, failed) in scan.tools.items():
            known_calls, known_last, known_failed = tools.get(name, (0, None, 0))
            newest = last if known_last is None else (known_last if last is None else max(last, known_last))
            tools[name] = (known_calls + calls, newest, known_failed + failed)
        add_model_usage(by_model, scan.by_model)
        for skill, usage in scan.by_skill.items():
            add_model_usage(by_skill.setdefault(skill, {}), usage)
        web_searches += scan.web_searches
        tool_calls += scan.tool_calls
        lines_added += scan.lines_added
        lines_removed += scan.lines_removed
        if scan.started_at is not None:
            started.append(scan.started_at)

    return UsageScan(
        by_model=by_model, by_skill=by_skill, web_searches=web_searches, started_at=min(started, default=None),
        tool_calls=tool_calls, lines_added=lines_added, lines_removed=lines_removed, tools=tools,
    )


def session_usage(main: UsageScan | None, agents: Mapping[str, UsageScan]) -> dict[str, Any]:
    """Return a session's usage totals, its per-model split and the parts behind them.

    Parameters
    ----------
    main : UsageScan or None
        The session's own transcript, or None while its first scan is pending.
    agents : mapping of str to UsageScan
        Each kind of subagent the session ran (its ``agentType``, ``''`` when
        unknown) with the summed usage of every agent of that kind.

    Returns
    -------
    dict
        ``usage`` (token totals over every model), ``usage_by_model`` (main
        conversation and subagents together, which is what the cost is priced
        from) and ``usage_parts``: ``main`` per model, ``agents`` per kind and
        model, ``skills`` per skill and model (main and subagents together),
        and ``web_searches``.
    """
    main_scan = main or UsageScan()
    total = merge_scans([main_scan, *agents.values()])

    usage: dict[str, int] = {}
    for model_usage in total.by_model.values():
        for key, value in model_usage.items():
            usage[key] = usage.get(key, 0) + value

    agent_parts: dict[str, ModelUsage] = {}
    for kind, scan in agents.items():
        add_model_usage(agent_parts.setdefault(kind, {}), scan.by_model)

    return {
        'usage': usage,
        'usage_by_model': total.by_model,
        'usage_parts': {
            'main': _copy_model_usage(main_scan.by_model),
            'agents': agent_parts,
            'skills': total.by_skill,
            'web_searches': total.web_searches,
        },
    }


def _copy_model_usage(source: Mapping[str, Mapping[str, int]]) -> ModelUsage:
    """Return a copy of *source* that shares no dict with it."""
    copy: ModelUsage = {}
    add_model_usage(copy, source)
    return copy
