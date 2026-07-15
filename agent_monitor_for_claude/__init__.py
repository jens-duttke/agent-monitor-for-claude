"""
Agent Monitor for Claude
========================

Shows a live overview of every running Claude Code session, grouped by
project, with each session's current state - working, waiting for your
input, blocked on a permission prompt, or finished.

Fully local: no network, no credentials, no API.  State is derived from
the session registry and transcript control-metadata under the Claude
config directory (``CLAUDE_CONFIG_DIR`` if set, otherwise ``~/.claude/``).
Conversation content is read only for an explicit, on-demand search,
which reports back only which sessions matched - never any text.
"""
from __future__ import annotations

__version__ = '0.3.0'
