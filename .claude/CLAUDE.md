# Project Guidelines

Local viewer of running Claude Code sessions (live status per project, on-demand History, content search). Apply Python best practices and clean code; only change code relevant to the prompt. Features come first: when a feature needs a new read, write, program, launch surface or dependency, build it and document it in `PRIVACY.md` - that file describes the tool, it never vetoes a feature. Rationale for most rules lives in the docstring or comment next to the code; read it before changing an order, guard or threshold.

## Commands
- Always use the project venv (`.venv\Scripts\python` on Windows, `.venv/bin/python` on Linux, created with `--system-site-packages` there).
- Both suites must pass after any change: `python -m unittest discover -s tests` and `node --test "tests/js/*.test.js"`. Fix the code, never weaken or remove a test; cover edge cases (missing/renamed fields, empty transcripts, dead PIDs, UTC vs local).
- Load-bearing, never weaken: `tests/test_transcript_privacy.py`, `tests/test_search.py`, `tests/test_session_delete.py`, `tests/test_tasks.py`, and the "HTML safety" group in `tests/js/logic.test.js`.

## Platform
- Windows and Linux, never macOS. Python >= 3.10.
- `__main__.py`'s version check stays above the package imports, and `__init__.py` imports nothing beyond `__future__` - an import sorter breaks both silently (`tests/test_main.py`).
- Only `platforms/` may import `ctypes.windll`, `ctypes.wintypes`, `winreg`, `fcntl` or `gi`. Platform checks (`sys.platform`, and `os.name` counts as one) live only in `platforms/__init__.py`, `process_probe.py` and `single_instance.py`; everywhere else ask `platforms.IS_WINDOWS`.
- `platforms/__init__.py` and what it imports must not import `i18n` or `settings` (import cycle; `tests/test_platforms_imports.py`). `platforms/linux.py` must import without any GUI toolkit (`gi` imported inside functions).
- A capability one OS lacks is an explicit no-op returning `False` with a docstring saying why, never a silent omission.

## Architecture
- Python is a pure data provider: `snapshot.build_snapshot()` returns raw per-session records; `ui/logic.js` derives status, labels, grouping, sorting and cost. Python never classifies. Keep `logic.js` DOM- and IO-free so Node can test it.
- Status is structural, never inferred from silence: a thinking phase writes nothing for minutes. "Your turn" means the newest entry is an `end_turn`, the interrupt marker or a local command's output. The only time inputs are `stalled` (only while a `tool_use` is unanswered), deadlines Claude Code writes (loop wakeup, usage-limit reset) and the turn-vs-process-start comparison of a reopened session. Extend `tests/js/logic.test.js` on every status change.
- A trailing interrupt or API error is definitive (beats a pending tool and a lagging registry `busy`/`idle`). After a stop or reopen (`turnStopped`), running subagent/workflow counts are phantoms - never promote or badge on them; only `child_count` survives.
- Whole texts (current call, reply, agent task/result, activity steps) are fetched per selection through a bridge method, never put on the per-poll snapshot. Graph-only fields ship only with `get_snapshot(graph)`.
- Read settings with `current_settings()` at the point of use, never `from .settings import <value>` (freezes it). That store and `i18n.T` are the only module-level mutable state; `T` is imported by reference - refill it in place, never rebind.
- A new setting touches: a `Settings` field, a `SettingSpec`, a `logic.SETTING_LABEL_KEYS` pair with keys in every locale, a row in `docs/configuration.md`, a mock spec in `dev-mock.js`.

## Claude Code internals (undocumented, change without notice)
- The registry (`sessions/*.json`), the `cwd` -> slug scheme and the transcript JSONL schema are unversioned. Parse defensively: `(d.get(k) or default)` plus `isinstance`; a bad field yields `unknown` or a skipped record, never a crash.
- Claude Code writes several facts in more than one shape (permission mode, local-command record, `procStart` epoch, WSL `procStart`); handle every known shape, never branch per entrypoint.
- Confine format knowledge: paths and `cwd_to_slug` in `paths.py`, registry fields in `sessions.py`, transcript fields in `transcript.py` (`subagents.py`, `search.py`, `tasks.py` read only what they need).
- One session can own many registry records; any per-record rule runs after `snapshot._one_record_per_session`. A worktree session's registry `cwd` is not its project - go through `worktrees.resolve_worktree`.
- `total_tokens_reminder` is a session token budget, not the context window; the window comes from `model_catalog.py`.

## Data, privacy and security (full statement: PRIVACY.md)
- Read what a feature needs, from Claude Code's files or anywhere local; prefer the smallest field because it is cheaper, not because larger reads are forbidden. No workarounds to avoid a needed read.
- Snapshot and history carry control values plus a few clipped display fields that mirror Claude Code's own UI (title, current tool, last reply, plan items, messages, error/retry/refusal/hook lines). Conversation text beyond that goes only through the bounded search excerpts and the on-demand detail bridges. `tests/test_transcript_privacy.py` guards against accidental broad reads - extend it deliberately when a feature adds a field.
- Writes only: the browser profile (`platforms.storage_dir`), the settings file (`settings.save_settings`, on Save), past-session deletion (`session_delete.py`), the Linux lock file; plus the clipboard on click. `~/.claude` is never written.
- A destructive action needs a validated target (UUID), path confinement with `relative_to`, a refusal while live (a probe error counts as live), and an explicit click plus an in-UI confirmation that names the target and focuses Cancel.
- Network: none except the opt-in update check (`update_check.py`, off by default). The only `socket` use is the localhost port probe in `app._http_port`. No credentials, ever.
- External programs: only `git` (through `git_status._git`) and `wsl.exe` by absolute System32 path - fixed arguments, validated cwd, never on the poll path. Nothing runs inside a distro; a stopped distro is never touched.
- A launched URI is a top-level template constant with a strictly validated interpolation. A remote address in the UI is an `<a target="_blank">` (load-bearing: a same-window navigation replaces the app) whose untrusted parts pass a validator (`changelogUrl`, `appReleaseUrl`, `pullRequestUrl`, `branchWebUrl`).
- No `eval`, `exec`, `compile`, dynamic imports, obfuscation or base64 tokens. Python pushes into the page only via `window.run_js` with a JSON literal.
- After any read/write/launch/network change, update `PRIVACY.md` and re-run its "Verify it yourself" greps so the documented hits still match.
- Side effects live in one module each (`git_status.py`, `process_probe.py`, `window_focus.py`, `settings.py`, `session_delete.py`, ...); helpers stay pure.
- Minimal dependencies (`pywebview`, `psutil`, `msgspec`, `regex`; PyGObject/WebKitGTK on Linux); a new one needs a check of the spec's `hiddenimports`/`excludes`. Prefer `ctypes` where it does the job.

## UI and window
- Never assign a WinForms property (e.g. `TopMost`) from a bridge thread - pythonnet keeps the GIL and the app deadlocks; marshal through `Invoke` (see `platforms.set_keep_on_top`). Stubbed tests cannot catch this.
- `prepare_gui_environment()` runs before anything touches the GUI toolkit, dialogs included.
- The controller scripts share one global scope, loaded in `boot-scripts.js` order: never declare a top-level name in two files (a duplicate `function` silently replaces the other); a new script goes into `boot-scripts.js` and the spec `datas`. `dev-mock.js` holds invented data only and never ships.
- CSP (`index.html`): no inline `<script>`, no `style=` in markup strings (set `el.style.*` from JS), no `blob:` or remote resource, `data:` only for images via `logic.mediaDataUrl`. `'unsafe-eval'` exists only for pywebview's bridge on WebKitGTK - application code never uses `eval`, `new Function` or string timers; use `window.run_js`, not `evaluate_js`.
- Markup strings: every interpolated value, numbers included, goes through `esc` (text) or `attr(name, value)` (a whole attribute, literal name); otherwise use `textContent`/`dataset`. Render by keyed reconciliation, never a full `innerHTML` rebuild.
- Any lookup keyed by on-disk data is a `Map`, never an object literal (`constructor` would resolve).
- Never use the native `title` tooltip; use `data-tip`. Anything appended to `body` at runtime is `position: fixed` and is reconciled at the end of `render` like `syncTooltip`/`syncOpenMenu`.
- CSS uses native nesting with `&`, 2-3 levels deep; every `user-select` carries a `-webkit-user-select` twin above it (WebKitGTK).
- Dialog buttons: the confirming button left of Cancel, both right-aligned, on every OS.

## Python style
- Module docstring first (title with `=` underline, blank line, description), then `from __future__ import annotations`.
- Type hints in signatures only, never in docstrings. numpydoc docstrings for public functions, classes and non-trivial methods. Never mention changes or improvements in comments or docstrings.
- `# type: ignore` only with a code and reason: `# type: ignore[code]  # reason`.
- PEP 8 with lines up to 140-160 characters; signatures and calls on one line when reasonable; 4-space continuation indent, never aligned to an opening bracket.
- Single quotes by default, double when the string contains a single quote, `"""` for docstrings. Hyphens (`-`) for dashes in text, never em or en dashes.
- Two blank lines between top-level definitions, one between methods; blank lines between logical blocks.
- Imports in three groups (stdlib, third-party, local), `import` before `from ... import`, alphabetical. Relative imports in the package, except `__main__.py` (absolute, for PyInstaller).
- Main exported functions first, then helpers; `_`-prefixed private helpers; `__all__` in library modules. Prefer functions over classes; no global mutable state (see the settings exception above). Descriptive names.
- No complex comprehensions with multiple conditions; use a loop with guard clauses. Validate inputs at the top with early returns.

## Build, docs, release, git
- New data file (locale, UI asset) -> spec `datas` (never `dev-mock.js`). New platform backend -> Windows module in `hiddenimports`, Linux module in `excludes`. The EXE is Windows only; on Linux the app runs from source.
- Keep in sync on user-facing change: the README feature list, `docs/configuration.md` (settings), `PRIVACY.md` (surfaces), the locale list. Every locale carries exactly the `en.json` keyset.
- Markdown outside `.claude/` is written for end users only: no internal names, measurements or design history.
- `CHANGELOG.md` for every user-facing change under `## [Unreleased]`; never mention CLAUDE.md in changelog entries or commit messages.
- Releases: use `/release`.
- Commit, tag and push only when asked; prefer a new commit over amending; no force-push or history rewrite without an explicit instruction.
- Never write to the auto-memory system; persistent knowledge belongs in this file.
