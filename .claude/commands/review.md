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

### Security & Privacy (especially changes touching `transcript.py`, `sessions.py`, `process_probe.py`, `platforms/`)
Features come first: a new read, write, external program, launch surface or dependency is fine when the change needs it. What this section checks is that every such step is **intended and documented**, and that untrusted data is handled safely - not whether the tool may do it at all.
- **New surfaces are deliberate and described:** a newly read field or file, a new write, a new program run, a new launch or address - does the change need it for its stated goal, and is it described in `PRIVACY.md` and CLAUDE.md's Security & Transparency section? An undocumented surface is a finding; a documented one is not.
- **Nothing leaves the machine unasked:** a new network destination, socket or upload is only fine when it is the feature itself and documented; anything that sends local data somewhere as a side effect is a finding.
- **No credentials:** nothing reads authentication tokens or other secrets.
- **Destructive actions stay guarded:** a delete or overwrite of user data validates its target, confines its paths, refuses anything still live, and runs only on an explicit user action - like `session_delete.delete_session`. Weakening those guards is a finding.
- **Untrusted data stays inert:** on-disk values reaching markup go through `esc`/`attr`, addresses are validated before they reach an `href`, a new program gets fixed arguments and a validated working directory.
- No `eval()`, `exec()`, `compile()` of data, dynamic imports, obfuscation, or base64-encoded strings.
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
- **Platform layer respected?** No `ctypes.windll`, `ctypes.wintypes`, `winreg`, `fcntl` or `gi` import outside `platforms/`, and no `sys.platform` check outside the three dispatch modules (`platforms/__init__.py`, `process_probe.py`, `single_instance.py`).

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
- **Does `test_transcript_privacy.py` match what the code reads?** If transcript parsing changed, update it deliberately for the new fields, so it keeps catching an *accidental* broad read.
- Do the `status.classify` table tests still cover every branch?
- Does a change to one platform backend have a counterpart in the other, or a documented reason it cannot?
- Run `python -m unittest discover -s tests` to verify all tests pass. Tests for the other operating system's backend skip themselves; a green run covers only the system you are on.

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
