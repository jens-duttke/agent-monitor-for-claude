"""
Agent Monitor for Claude
========================

Shows a live overview of every running Claude Code session, grouped by
project, with each session's current state - working, waiting for your
input, blocked on a permission prompt, or finished.

Fully local: no network, no credentials, no API.  State is derived mainly
from the session registry and transcript metadata under the Claude config
directory (``CLAUDE_CONFIG_DIR`` if set, otherwise ``~/.claude/``).  Some
conversation text is shown locally - a session's current tool call and last
reply, its activity and its subagents' tasks and results in the details, and
a few short excerpts around each content-search hit - but nothing ever leaves
the machine; see PRIVACY.md.
"""
from __future__ import annotations

__version__ = '1.0.0'
