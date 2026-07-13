---
allowed-tools: Read, Edit, Bash, Grep, Glob, Agent
description: Critical review of all staged changes, documentation updates, and final cleanup
---

Perform a systematic quality review of all staged changes. Work through the following steps **sequentially**. Use the full context of this conversation to understand WHY the changes were made.

## Step 1: Critical Code Review

Run `git diff --staged` and critically review EVERY changed file:

### Correctness & Logic
- Are all changes meaningful and actually necessary for the intended implementation?
- Off-by-one errors, boundary conditions, edge cases (empty inputs, None values, missing/renamed on-disk fields)?
- State management - can objects be left in invalid states?
- Order dependencies - do operations assume a specific sequence without enforcing it?

### Code Quality
- Can existing code be simplified or removed (dead code, duplicates)?
- Is there optimization potential (readability, maintainability, poll cost)?
- Are naming conventions and code style consistent with CLAUDE.md rules?
- When a comment is corrected because behavior changed, check whether the same outdated concept is encoded in nearby variable, parameter, or function names - and rename them too.
- No ambiguous names like `other`, `data2`, `flag`.

### Security & Privacy (especially changes touching `transcript.py`, `sessions.py`, `process_probe.py`)
- **Privacy boundary:** does any code path read, return, store, log, or render conversation content - message `text`, `thinking` blocks, tool `input`, or tool-result `content`? Only control metadata (entry type, `stop_reason`, tool IDs, tool name, timestamps) may be extracted.
- **No network:** no sockets, no `requests`, no URL literals, no external destinations of any kind.
- **No credentials:** nothing reads authentication tokens or other secrets.
- **Read-only bar two sanctioned write surfaces:** no file or registry writes except (1) the WebView2 UI-preference profile and (2) `session_delete.delete_session` removing a past session's own files under `projects/` (guarded by a UUID check, a live-process refusal, and `projects/` path confinement, only on an explicit user action). Any other write - or any weakening of those guards - is a finding.
- No `eval()`, `exec()`, `compile()`, dynamic imports, obfuscation, or base64-encoded strings.
- **Defensive parsing:** unversioned Claude Code internals (registry, transcript schema, slug scheme) must degrade to `unknown`/skip on a missing or mistyped field, never crash.

### Concurrency & Resource Management
- Race conditions in threading code (the poll loop, the webview thread)?
- Proper cleanup of resources in error paths (file handles, context managers)?
- Timeout / non-blocking handling on process probing so the snapshot stays responsive?

### Error Handling & Resilience
- Input validation at function boundaries?
- Early returns and guard clauses used consistently?
- Error messages provide enough context without leaking file contents?

### Type Safety & Imports
- `from __future__ import annotations` present as the first import (after the module docstring)?
- Type hints in function signatures (not in docstrings)?
- Import grouping correct (stdlib / third-party / local), relative imports within the package?
- No circular dependencies, no unused imports?

### Style & Formatting (per CLAUDE.md)
- Single quotes default, double when containing singles, triple-double for docstrings?
- Hyphens for dashes, never em or en dashes?
- PEP8-based with 140-160 char line length?
- No deep indentation aligning with opening brackets?
- `# type: ignore` only with a specific error code and short reason?

Summarize your findings before moving to the next step.

## Step 2: Documentation Updates

> Note: do NOT check `CHANGELOG.md` and do NOT flag a missing changelog entry - it is handled outside `/review`.

### README.md
- If features were added, changed, or removed: is README.md updated to reflect this?
- Is the feature list accurate? Are descriptions still correct?
- If locale files changed: is the language list in sync?

### docs/configuration.md
- If settings were added, changed, or removed: is `docs/configuration.md` updated?

### CLAUDE.md
- Does CLAUDE.md need updates based on insights from this conversation (new conventions, changed structure or dependencies, important architectural decisions)?

Apply necessary changes directly.

## Step 3: Test Coverage Check

- Does every new function or changed behavior have corresponding tests in `tests/`?
- Are edge cases covered (missing/renamed fields, empty transcripts, dead PIDs, UTC-vs-local age)?
- **Is the privacy boundary still enforced?** If transcript parsing changed, does `test_transcript_privacy.py` still prove no content leaks - and was it extended for the new fields?
- Do the `status.classify` table tests still cover every branch?
- Run `python -m unittest discover -s tests` to verify all tests pass.

If tests are missing or failing, fix them directly.

## Step 4: Final Cleanup

Run `git diff --staged` again and review ALL staged files one last time:

- Is ONLY code present/changed that is actually necessary for the intended implementation?
- No accidental debug logs, `print()` statements, commented-out code blocks, or leftover TODO comments?
- No unintended formatting or whitespace-only changes?
- No changes to files unrelated to the current feature?
- No `# type: ignore` without a specific error code?
- No docstrings or comments mentioning "changes", "improvements", or "type hints"?

If you find issues in this step, fix them directly.

## Summary

Provide a brief summary:
1. What was reviewed
2. Which issues were found and fixed
3. Which documentation was updated
4. Whether the staged changes are now ready to commit

Do NOT commit directly. If the changes are ready, run the `/commit-message` slash command to generate a properly formatted commit message.
