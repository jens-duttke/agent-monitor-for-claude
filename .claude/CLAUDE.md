# Project Guidelines

Apply Python best practices and clean code principles. Only change code relevant to the prompt.
Prioritize readability and auditability - this tool reads local Claude Code data, and users must be able to verify at a glance that it never touches conversation content, never writes, and never talks to the network.

## Platform
- Windows-only application - no `sys.platform` checks or cross-platform guards needed
- Windows APIs (`ctypes.windll`, `winreg`) can be used unconditionally

## Purpose & Scope
- Local, read-only viewer of running Claude Code sessions grouped by project, with each session's live status (working / waiting for you / permission needed / finished)
- Fully offline: no network, no credentials, no API. State is derived only from the local session registry and transcript control-metadata under `CLAUDE_CONFIG_DIR` (default `~/.claude/`)
- The single backend entry point for the UI is `snapshot.build_snapshot()` - it returns a flat list of **raw** per-session records; the UI (`ui/logic.js`) derives status, labels, grouping and sorting. Everything returned is JSON-serializable and content-free

## Data & Privacy (pragmatic, not dogmatic)
- What actually keeps this safe: it is **fully local** (no network, nothing leaves the machine), reads **no credentials**, and is **read-only** (bar the WebView2 UI-preference profile). Those guarantees are non-negotiable - see Security & Transparency.
- Within that, read whatever a feature genuinely needs from the local Claude Code files - but **only what it needs**, not gratuitously. Prefer the smallest field that answers the question (a `stop_reason` over a whole message, a `meta.json` `description` over a subagent transcript). Don't build elaborate workarounds to avoid reading a field a feature legitimately requires.
- `transcript.py` reads mostly control-flow metadata (entry `type`, `stop_reason`, tool IDs/name, timestamps, `message.model`, `message.usage`, `permissionMode`) plus a few display fields that mirror Claude Code's own UI: the session title (`customTitle` > `aiTitle` > first prompt via `_prompt_display_text`, stripped and truncated) and subagent `description`s. That is fine - it is shown locally, exactly as Claude Code shows it, and never transmitted.
- `tests/test_transcript_privacy.py` still guards against *accidental* broad reads: markers planted in message text / tool results must not leak into the snapshot, so a careless "dump the whole entry" change gets caught. Keep it as a guard, not as a purity mandate - update it deliberately when a feature needs a new field.

## Claude Code Internals (unversioned - parse defensively)
- The session registry (`~/.claude/sessions/*.json`), the `cwd` -> project-slug scheme, and the transcript JSONL schema are undocumented Claude Code internals that can change without notice
- All parsing degrades gracefully: a missing, renamed, or mistyped field yields status `unknown` or a skipped record - never a crash. Use `(data.get(...) or default)` and `isinstance` checks
- The registry's `procStart` (.NET ticks of the **local** wall clock) is validated against the live process start time in `process_probe` - a mismatch means Windows recycled the PID and the record is stale (session reported as not alive). `startedAt` (epoch ms) supplies the displayed age for `NEW` sessions that have no transcript yet
- Confine this knowledge: file locations and the `cwd_to_slug` mapping live in `paths.py`; transcript field names in `transcript.py`; registry field names in `sessions.py`. Keep it that way

## Status Model (canonical reference)
- **Status derivation lives in the UI, not Python.** `snapshot.build_snapshot()` returns raw per-session records; `ui/logic.js` derives the status. Python never classifies. Status values (strings): `working`, `processing`, `interrupted`, `errored`, `awaiting_input`, `awaiting_permission`, `new`, `completed`, `unknown`
- **Guiding principle (structural, not time-based):** two states mean "your turn", both read from *what the newest entry is*, never from elapsed time - an assistant turn that ended with `end_turn` (the model finished, status `awaiting_input`/"Idle"), and the fixed **interrupt marker** (`[Request interrupted by user]`, surfaced by the parser as `last_entry_kind === 'user_interrupt'`; the user stopped the turn, so control is back with them and the model owes nothing). The interrupt is its **own** status `interrupted` (yellow, own filter chip), not folded into `awaiting_input`, so a session abandoned mid-task is told apart from a clean finish. In every other live state the model owes a response and is therefore `working` - a just-sent user prompt, a `tool_result` it is still reasoning about, or a long silent **thinking** phase. Elapsed silence must never read as "done": thinking writes nothing to the transcript for minutes, so a five-minute think looks identical on disk to an idle session. There is deliberately no freshness/time input. This is also why the interrupt is detected by its marker and not by time: on disk an interrupt marker is just another `user` turn, indistinguishable from a fresh prompt, so a time rule would flip thinking sessions to "your turn" - exactly the earlier `fresh`-window heuristic (flipped after ~90 s) that this replaced. A **local command** (a slash/`!` command that runs outside the model) is the one other non-`working` structural signal: its `system`/`local_command` record means no reply is owed, so the trailing command entry reads `awaiting_input` instead of a pending prompt. (An even earlier CPU/IO heuristic was removed too - the VS Code extension host produces background I/O in the idle `claude` process.)
- `logic.classify()` is pure and tested (`tests/js/logic.test.js`), in order:
  - process not alive -> `completed`
  - no transcript (fresh window, no prompt yet) -> `new`
  - newest entry is the interrupt marker (`last_entry_kind === 'user_interrupt'`) -> `interrupted`. Checked **before** the pending-tool rule: an interrupt can leave a `tool_use` unresolved, but the trailing marker means the whole turn was stopped, so it wins
  - newest entry is an API-error turn (`last_entry_kind === 'api_error'`, a trailing `isApiErrorMessage` assistant entry) -> `errored`. Also checked **before** the pending-tool rule: the error stopped the turn even if it left a `tool_use` unresolved. The parser also flags whether that error is a usage/session limit (`usage_limited`, HTTP 429 or `error === 'rate_limit'`) so the label can name it (`status_usage_limit`, "Usage limit reached") versus a generic API error (`status_errored`, "Error"). A mid-conversation error the CLI retried is superseded by the later real turn, so only a *trailing* error reads as `errored`
  - pending tool (last `tool_use` has no matching `tool_result`): `awaiting_permission` when blocking, else `working`. `logic.pendingIsBlocking(toolName, permissionMode)`: question/plan dialogs (`AskUserQuestion`, `Exit/EnterPlanMode`) block in every mode; a generic tool blocks only in a **prompting mode** (`permissionMode === 'default'`). In `auto`/`acceptEdits`/`plan` (and unknown), a pending generic tool is executing, never a prompt. A child process (`child_count > 0`) also means executing, not blocking
  - newest entry is an assistant `end_turn` -> `awaiting_input`
  - newest entry is a **local command** (`last_entry_kind === 'local_command'`, from a slash/`!` command's `system`/`local_command` execution record) -> `awaiting_input`. A local command runs outside the model - Claude Code even writes a caveat telling it not to respond - so no reply is owed; without this rule the trailing command `user` entry would misread as a pending prompt and stick on `working`. It can briefly read idle while a prompt-style command's first turn is still being thought out, but that is transient, unlike a permanently stuck `working`
  - newest entry is `user_text` / `tool_result` / a non-`end_turn` assistant turn -> `working`
  - otherwise `awaiting_input` if there was any activity, else `unknown`
- `logic.refineWithNative()` applies the registry's native `status` field after `classify`: `busy` -> `working`, `idle` -> `awaiting_input`, and `waiting` (only when the registry also gives a `waitingFor` reason) -> `awaiting_permission`; never overrides `awaiting_permission`, `interrupted`, or `errored` (all definitive - the interrupt marker or an API-error turn being newest means no turn is running, so a lagging registry `busy`/`idle` must not flip or flatten it), never demotes `new`. The `waiting` case is essential because Claude Code marks a session blocked on a permission prompt *before* the pending `tool_use` reaches the transcript, so the structural rule still sees the fresh user prompt and would read `working`; the registry is authoritative that the agent is blocked on you. `waiting_for` is read in `sessions.py` (registry field `waitingFor`) and passed through the snapshot. Also essential for worktree sessions where two processes share one transcript. Note: VS Code sessions usually have **no** native status (`null`), which is exactly why the structural rule above (not the registry) must detect thinking
- `logic.refineWithBackgroundWork()` promotes `awaiting_input`/`unknown`/`interrupted` -> `processing` when a subagent is running or the session has a child process. So a finished turn with work still running in the background does not read as "your turn". Force-stop caveat: after a force-stopped turn (`user_interrupt` or `api_error` - a usage limit stops the whole CLI), `deriveStatus` and `buildSession` treat `subagents_running` as 0 - in-process subagents die with the stop, so a still-"running" count is a phantom the recent window has yet to clear and must not promote the session (nor show a running badge). A detached OS **child process** can outlive the stop, so `child_count` still counts; an interrupted session with real surviving background work does read as `processing`, whereas `errored` is deliberately **not** in the promotable set (a stuck-on-error session stays `errored` so the error stays the salient signal, never masked as `processing`)
- The permission mode comes from the latest `permission-mode` entry (`permissionMode`, tracked in the incremental scanner in `transcript.py`); absent -> treated as non-prompting. Shown per session via `logic.modeLabel`
- The tail parser (`transcript.py`) escalates its window (256 KB -> 2 MB -> 16 MB) when nothing parses; sidechain entries (`isSidechain: true`) are skipped for state, model, and title - only usage counts them. Injected `isMeta` entries (the local-command "DO NOT respond" caveat, continuation summaries) are likewise skipped for state, so an injected notice never reads as a pending prompt
- **Subagents** (`subagents.py`): counts running and recently-finished subagents per session from `projects/<slug>/<session>/subagents/` (recursively, incl. `workflows/<wf>/`). Running = an `agent-*.jsonl` within `SUBAGENT_RECENT_SECONDS` whose transcript has **not** ended with `end_turn`; finished = ended. Reads file timestamps, the tail's last `stop_reason`, and each `meta.json`'s `agentType`/`description`
- **Background processes** (`process_probe`): `child_count`/`child_names` are the session's meaningful descendant processes (excluding `conhost.exe`) - a watched build/scan. OS processes, **not** subagents (which run in-process); the two badges are distinct
- "Needs your attention" = `awaiting_input` union `awaiting_permission` union `interrupted` union `errored` (`logic.needsAttention`)
- Labels are tone-accurate: `awaiting_input` reads as "Idle" (the agent finished; nothing is mandatory) with a calmer text color, and `awaiting_permission` is refined by the pending tool's name (`logic.attentionLabel`): `AskUserQuestion` -> question dialog, plan-mode tools -> plan review, any other known tool -> permission prompt. When `awaiting_permission` comes from the registry `waiting` signal there is **no** pending tool to name (and `waitingFor` reports `permission prompt` even for a plain question), so the label stays neutral (`status_needs_you`, "Waiting for you") rather than claiming a permission is needed. There is no separate legend: the toolbar has one filter chip per status color (`FILTER_DEFS`, mapped by `logic.filterBucket`) and each chip's dot doubles as the color key. The whole `awaiting_permission` band lives under the "Needs you" chip (`filter_needs`, red dot); `errored` under its own "Error" chip (`filter_errored`, orange dot); `interrupted` under its own "Interrupted" chip (`filter_interrupted`, yellow dot - the warm attention colours run a traffic-light gradient red -> orange -> yellow -> green from blocked through errored and interrupted to idle), `awaiting_input` under "Idle" (green), `processing` under "Background", `completed`/`unknown` under "Quiet", and `new` under its own "New" chip (`FILTER_DEFS` label `status_new`, `dot-new` colour) - a regular chip like the rest, on by default and unchecked to hide, not a separate visibility toggle. `interrupted` is a "your turn" state (it stays in the quiet `STATUS_BAND` for project ordering, like `awaiting_input`) but keeps its own colour and chip so an abandoned-mid-task session is spotted at a glance and can be filtered on its own; `errored` behaves the same way (own colour and chip, quiet `STATUS_BAND`) so a session stuck on a usage limit or API error stands out too
- Age is computed in Python (`transcript._activity_age`) from the newest transcript entry's `timestamp`, parsed to POSIX seconds in `_timestamp_epoch`: Claude Code records timestamps in UTC, so the trailing `Z` is normalized to an explicit offset (a value with no offset is read as UTC) and the result compares directly to `time.time()`. It falls back to the file mtime **only** when no entry carries a parseable timestamp. Deriving from the entry timestamp rather than the mtime is deliberate: an idle process (e.g. the VS Code extension host) that rewrites session metadata in place bumps the file mtime without appending a turn, and that must not reset the "last activity" age. `build_snapshot` ships the numeric age; the UI formats and ticks it
- `logic.classify()`/`deriveStatus()` take only plain data, no DOM/IO - keep them pure so they stay Node-testable

## Token Usage & Cost
- `transcript.py` sums usage across every assistant turn (subagents included) into an overall total **and** a per-model split (`usage_by_model`), because subagents often run on a cheaper model than the main session - a single rate on the combined total would be wrong. Cache-creation tokens are also split by TTL (`cache_creation_5m/1h_input_tokens`) from the nested `usage.cache_creation`, since 5m and 1h writes are priced differently
- Claude Code writes locally-generated assistant turns (interrupts, injected notices) with `message.model == "<synthetic>"` and zero usage. That sentinel is **not a real model**: `transcript.py` (`_SYNTHETIC_MODEL`) excludes it from the displayed `model`, the per-model split, and the history, and `logic.sessionCostUsd` also skips any zero-usage model so a placeholder can never force the whole session to the token-total fallback
- `transcript.py` also builds `model_timeline` - the **main conversation**'s chronological model-switch log (sidechain/subagent turns and the synthetic sentinel excluded, unlike `usage_by_model`). Each main-conversation assistant turn is a `(timestamp, model)` event; `_model_timeline` sorts them by time (transcript entries are not strictly ordered on disk, so order is resolved here) and run-length compresses into one `{time, model}` entry per contiguous run. So a model left and returned to appears **more than once** and the *last* entry is the current model with the time it was switched back to - unlike the earlier `model_first_seen` (per-model minimum), which collapsed the return and could leave a no-longer-used model as the newest entry. Only these few switch points cross the bridge (reduced Python-side, like the old field). `logic.modelHistory` just formats labels (`formatModel`), keeping the order; `buildSession` sets `model_switched` (>1 run)/`model_history`; the UI shows "(+N)" after the model, hover listing each run's model and switch time (via `fmtDateTime`)
- Prices live in **`pricing.json`** at the repo root (loaded by `pricing.py`, mirroring the `i18n.py` `LOCALE_DIR` pattern; shipped verbatim in the bootstrap payload, bundled via the spec `datas`; mock-supplied in `dev-mock.js`). It is a **hand-maintained offline snapshot** - no network, no live lookup, no pricing URL in code; a `_comment` key documents the format and is stripped on load. Top-level keys are **effective-from dates** (`YYYY-MM-DD`); each is a complete snapshot of `model-key -> {input, output, cache_read, cache_write_5m, cache_write_1h}` in $/MTok (**explicit per-model rates, no multipliers** - the 5m/1h/read prices are listed, so they can differ from the base-input ratios). Model keys are family-version (`opus-4-8`, `sonnet-5`, `haiku-4-5`, `fable-5`, ...) since versions are priced differently (Opus 4.8 != 4.1, Sonnet 5 != 4.6)
- Cost is **derived in `ui/logic.js`**, not Python: `resolvePrices(schedules, dateStr)` picks the schedule with the latest date on or before today (so a future change entered ahead of time takes effect on its own; `index.js` passes the local date and threads the resolved table through `groupProjects`/`buildSession`). `modelPriceKey` maps a model id to its key (strips `claude-`, `[tier]`, trailing snapshot date). `sessionCostUsd` prices each model's usage separately; if **any** model has no rate in the resolved table it returns `null` and the UI shows a plain token total. The 1M-context tier is not modelled (long-context turns undercounted). Cache writes without a TTL split are priced at the 5m rate
- `formatCost` renders whole dollars (`$19`, rounded) or `<$1` below a dollar - the estimate is too coarse for cents or a "~". The row shows this compact cost (or the token-total fallback) by default; a >500ms hover on the usage figure animates it open (CSS grid `0fr`->`1fr`) to the full per-tier breakdown. `buildSession` supplies `usage_compact` (the anchor) and `usage_detail` (the breakdown that slides open before it); `usage_total` is the token count the "Usage" sort uses. Title and usage share one flex column so a long figure shrinks only that row's title

## Window & DPI
- The window is a normal pywebview + WebView2 window - no tray, no tray-anchored positioning, no `SetWindowPos`
- pywebview 6.x `resize()`/`move()` expect **logical pixels** (pywebview applies DPI scaling internally)
- The UI is a static asset bundle in `agent_monitor_for_claude/ui/` (`index.html`/`index.css`/`logic.js`/`index.js`); Python exposes raw data via the `js_api` bridge (`app._MonitorApi`). **Python is a pure data provider**: all derivation (status, formatting, grouping, sorting) lives in `ui/logic.js`, which is DOM-free so it runs in the browser and under Node for tests. `ui/index.js` owns the DOM/bridge side effects and renders via keyed reconciliation (reuse nodes, no full `innerHTML` rebuild) so open menus and scroll position survive a refresh. There is **one** UI HTML file (`index.html`) - no separate dev harness to drift. Opened directly in a browser (a `file://` page, or `index.html?mock`) there is no bridge, so `index.js` loads `ui/dev-mock.js` and renders from its fabricated showcase sessions instead - no Python needed. `dev-mock.js` holds **only** invented session data plus a tiny price snapshot (no labels - the full set lives in `index.js` `DEFAULT_LABELS`, so the preview can't drift), stays mock-only, is never wired to real session data, and is **deliberately excluded from the spec `datas`** so it never ships. New *shipping* UI asset files must be added to the `datas` list in the spec (but never `dev-mock.js`)
- **CSS uses native nesting** (the target is a single modern WebView2/Chromium, so it is fully supported - no build step, no prefixing). In `index.css`, nest a component's states, children, and variants under its base rule with `&` instead of repeating the parent selector across flat rules. Keep nesting shallow (2-3 levels) and readable; don't nest so deep that a selector's origin is hard to trace
- **Never use the browser's native `title` tooltip.** It is ugly, unthemed, and has an uncontrollable delay and placement. Always use the app's own HTML tooltip: give the element a `data-tip` attribute (newlines become line breaks) and the single delegated handler in `index.js` (`initTooltips`, `.tooltip` in the CSS) shows the shared, themed popup on hover. `document.title` (the window/tab title) is not a tooltip and is fine

## Security & Transparency
- **Read-only**: no file, registry, or any other write operations in application code. The sole sanctioned write surface is the WebView2 profile at `%LOCALAPPDATA%\AgentMonitorForClaude` (`webview.start(private_mode=False, storage_path=...)`), which persists localStorage UI preferences (theme, filter, collapsed panels); without it pywebview's private mode resets them every launch
- **No network**: no outbound connections of any kind. No `requests`, no URL literals, no external destinations. (pywebview's internal localhost HTTP server that delivers the bundled UI to its own window is the sole, sanctioned socket; with `private_mode=False` it uses pywebview's fixed default port so the localStorage origin stays stable across restarts)
- **No credentials**: never read authentication tokens or other secrets
- No `eval()`, `exec()`, `compile()`, or dynamic imports; no obfuscation, no base64-encoded strings or tokens
- **Window activation** (`window_focus.py`) is a sanctioned capability: Win32 window enumeration and `SetForegroundWindow`, executed only on an explicit user click. Window titles are compared in memory to pick the right window - never stored, logged, or displayed
- **Session deep link** (`window_focus.py`) is the second sanctioned launch surface: `os.startfile` on the official extension URI `vscode://anthropic.claude-code/open?session=<uuid>` (template is a top-level constant), only on user click, with the session id strictly validated as a UUID. No other URI scheme may ever be launched
- **Open project folder** (`window_focus.py`, `open_directory`) is the third sanctioned launch surface: `os.startfile` on a session's project `cwd`, only on user click (the panel-header path text), reached through the `open_path` bridge in `app.py`. The path is validated with `os.path.isdir` **before** it reaches the shell, so only a real directory is ever launched - a stale path, a file, or anything carrying a URI scheme is a silent no-op, never something the shell could execute. This is the only file-system path handed to `os.startfile`
- Minimal, well-known dependencies only: `pywebview`, `psutil`. Adding a dependency is a significant decision (see the pre-merge gate)
- Isolate side effects: process access in `process_probe.py`, file reads in `sessions.py`/`transcript.py`; keep helper and utility functions pure

## Type Hints & Documentation
- Module docstring as the very first element (title with equals underline, blank line, description)
- `from __future__ import annotations` as the first import (after the module docstring)
- Type hints in function signatures only, not in docstrings
- numpydoc (NumPy-style) docstrings for public functions, classes, and non-trivial methods; skip for trivial 1-3 line methods
- Never mention changes, improvements, or type hints in comments or docstrings
- `# type: ignore` only with a specific error code and short reason: `# type: ignore[code]  # reason`

## Formatting
- PEP8-based with an extended line length of 140-160 characters
- Function signatures and calls on one line when reasonable
- Never use deep indentation to align with a previous line's opening bracket; use 4-space indentation from the statement start
- Single quotes (`'`) default, double (`"`) when containing single quotes, triple-double (`"""`) for docstrings
- Use hyphens (`-`) for dashes in text, never em dashes or en dashes

## Spacing
- Two blank lines between top-level functions/classes, one between methods
- Blank lines separate logical blocks (after guards, before returns)

## Imports
- Three groups separated by blank lines: standard library, third-party, local
- Within groups: `import` before `from...import`, sorted alphabetically
- Relative imports within the `agent_monitor_for_claude` package, except `__main__.py` which uses absolute imports for PyInstaller compatibility

## Structure
- Main exported functions first, then helpers in logical order
- Prefix non-exported helpers with underscore; `__all__` for library modules
- Prefer functional/modular code over classes; no global mutable state
- Descriptive, self-explanatory names - no ambiguous names like `other`, `data2`, `flag`

## List Comprehensions
- Avoid complex comprehensions with multiple conditions or long expressions
- Use explicit loops with guard clauses when there are multiple conditions or repeated calls per item

## Validation & Errors
- Validate inputs at function start; early returns and guard clauses
- Defensively parse all on-disk / external data (see Claude Code Internals)

## Testing
- Two suites, both must pass after any change: the Python backend (`python -m unittest discover -s tests`) **and** the pure UI logic (`node --test tests/js/logic.test.js`, Node's built-in runner, no extra dependency)
- Fix the code to make tests pass - never weaken or remove tests to avoid failures
- Cover edge cases (missing/renamed fields, empty transcripts, dead PIDs, UTC-vs-local age), not just the happy path
- Load-bearing tests: the Python privacy test (`tests/test_transcript_privacy.py`) guards the content boundary; the status-classification table tests now live in `tests/js/logic.test.js` (they moved with the logic) - extend them when behavior changes. The thinking case (a stale `user_text` entry stays `working`) is explicitly covered there
- Python: `unittest` only (stdlib); mock time by patching `time.time()`/`datetime`; use temp dirs plus `CLAUDE_CONFIG_DIR` for file-based tests. JS: `node:test` + `node:assert`, `logic.js` only (no DOM), required into the test via `require('../../agent_monitor_for_claude/ui/logic.js')`
- Tests live in `tests/` (Python) and `tests/js/` (Node), both outside the package and not bundled

## PyInstaller / Build
- Spec file: `agent_monitor_for_claude.spec` - all build config lives there
- When adding new data files (locale files, UI assets): add them to the `datas` list in the spec
- When adding new imports: check whether PyInstaller detects them; if not, add to `hiddenimports`
- After any dependency change, verify the `excludes` list doesn't break a transitive import

## README / Docs
- Keep the feature list in `README.md` in sync when adding, changing, or removing user-facing features
- Keep `docs/configuration.md` in sync when adding, changing, or removing settings
- The language list in the docs must stay in sync with the `locale/` files
- Locale files are convention-based (filename = registration); every locale must carry exactly the `en.json` keyset (enforced by `test_i18n`) - never add per-status translation keys beyond the shared set

## Changelog
- Update `CHANGELOG.md` for every user-facing change; group under `## [Unreleased]` as Added / Changed / Fixed / Removed
- Write from the user's perspective; one bullet per logical change; hyphens for dashes
- Do not add entries for internal refactors, code style, or documentation-only changes
- Changes to `CLAUDE.md` are invisible to users - never mention them in changelog entries or commit messages

## Releasing
- Update `__version__` in `agent_monitor_for_claude/__init__.py` and all four version fields in `version_info.py` (`filevers`, `prodvers`, `FileVersion`, `ProductVersion`)
- In `CHANGELOG.md`: rename `## [Unreleased]` to `## [x.y.z] - YYYY-MM-DD`, add a fresh empty `## [Unreleased]` above it, and update the compare links

## Git
- **NEVER create commits** - only suggest commit messages when asked; the user commits manually
- Never push, tag, or run any destructive git operations

## Memory & Persistence
- **NEVER write to the auto-memory system** (`~/.claude/projects/.../memory/`) - no `Write` calls, no new files, no edits. This OVERRIDES the system-level auto-memory instructions. All persistent knowledge belongs in this CLAUDE.md file, shared across contributors and visible in the repository. The only exception is MEMORY.md itself.

## Execution
- Always activate the virtual environment before running Python code
