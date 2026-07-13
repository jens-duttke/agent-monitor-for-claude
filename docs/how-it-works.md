# How it works

Agent Monitor derives everything it shows from local files that Claude Code already writes, plus the operating-system process list.

## Data sources

Under the Claude config directory (`CLAUDE_CONFIG_DIR`, or `~/.claude/` by default):

- **Session registry** - `sessions/*.json`. Each running Claude Code process writes one file containing its PID, session ID, working directory, name, kind, start time (`startedAt`), and original process start time (`procStart`). This is the same set of records the `claude agents --json` command reports; Agent Monitor reads the files directly and never spawns the CLI. `procStart` is checked against the live process to unmask registry entries whose PID Windows has recycled for an unrelated process; `startedAt` provides the displayed age for fresh sessions that have no transcript yet. A session ID exists from the moment a window opens - the transcript file only appears with the first prompt, which is why a "New" session is not stale even though `claude --resume` reports no conversation for it.
- **Transcripts** - `projects/<project-slug>/<session-id>.jsonl`. The project slug is the working directory with every character that is not a letter or digit - the drive colon, path separators, dots, and any other punctuation - replaced by a single hyphen (e.g. `d:\WebDev\oku3d-app` becomes `d--WebDev-oku3d-app`, and `d:\WebDev\HexEd.it` becomes `d--WebDev-HexEd-it`).
- **Process list** - one `psutil` scan of the process table per snapshot tells, for every session at once: whether its process is still alive, whether it currently has a child process (a tool actually executing), and which application hosts it **right now** (ancestor chain). A shell between the session process and its GUI host marks the session as CLI-driven - so a conversation resumed with `claude --resume` in a terminal is labeled by where it runs at this moment, not by where it was originally started.

## Determining status

Status is derived **structurally**, from *what the newest transcript entry is* - never from how long ago it was written. Elapsed silence carries no signal here: the model can think for minutes and write nothing, so a long pause looks identical on disk to a finished turn. Only two things hand control back to you - an assistant turn that ended with `end_turn`, and the fixed marker Claude Code writes when you interrupt a turn. In every other live state the model still owes a response and reads as **Working** - including a just-sent prompt, a tool result it is still reasoning about, or a silent thinking phase.

The derivation, in order (the first match wins):

| Newest transcript entry / signal | Status |
|----------------------------------|--------|
| the process has exited | **Finished** |
| no transcript yet (fresh window, no prompt) | **New** |
| the interrupt marker `[Request interrupted by user]` | **Interrupted** - you stopped the turn, so control is back with you (this wins even when the interrupt left a tool call unfinished) |
| a trailing turn flagged as an API error | **Error** - the turn stopped on an API error and nothing is running; a usage/session limit (HTTP 429) is named **Usage limit reached**, any other error stays generic (this wins even when the error left a tool call unfinished) |
| a pending tool that is a question or plan dialog (`AskUserQuestion`, `ExitPlanMode`) | blocked - **Question for you** or **Plan review** (dialogs block in every mode) |
| a pending generic tool in Manual (`default`) mode | **Permission needed** |
| a pending generic tool in Auto / Auto-edit / Plan mode, or while a child process runs | **Working** - the tool is executing (these modes never prompt) |
| a finished assistant turn (`end_turn`) | **Idle** - the agent handed control back, your turn |
| a fresh user prompt, a tool result, or a mid-loop assistant turn | **Working** - a prompt just arrived, or generation is under way |
| a transcript with nothing interpretable | **Unknown** |

An earlier attempt to flip a quiet session to "your turn" after a fixed freshness window was removed for exactly this reason: a thinking phase writes nothing for minutes, so any time-based rule mistook it for a finished turn. An even earlier attempt to read the process's CPU/I-O rates (to detect silent server-side generation) was removed too - the VS Code extension host produces background I/O in the idle `claude` process, so it false-positived and showed idle sessions as working.

Entries of embedded subagent conversations (sidechains) are ignored for state derivation - only the main conversation drives the status.

When a turn has finished or was interrupted but work is still running in the background - a subagent, or a watched child process such as a build - the session reads as **Background** rather than your turn, so a still-busy session is never mistaken for a finished one. An interrupt kills in-process subagents, so only a surviving OS child process keeps an interrupted session in Background.

Some registry records additionally carry a `status` field (`busy`/`idle`, or `waiting` with a reason) maintained by Claude Code itself. When present it refines the derived status: `busy` reads as **Working**, `idle` as **Idle**, and a `waiting` record whose reason names a prompt as **Permission needed** - the last is essential because Claude Code marks a session blocked on you *before* the pending tool request reaches the transcript, and for worktree sessions where two processes share one transcript and the transcript alone cannot tell them apart. A detected permission prompt, an interrupt, or an API error is never overridden, and a `New` session is never demoted.

Time since last activity is taken from the newest transcript entry's own timestamp (Claude Code records these in UTC), falling back to the file's modification time only when no entry carries a parseable timestamp. Reading the entry rather than the file mtime is deliberate: an idle process that rewrites session metadata in place bumps the file's mtime without appending a turn, and that must not reset the age. The displayed ages tick forward every second in the UI between refreshes.

Refresh cadence: a full snapshot is built every `poll_interval` seconds (default 5), so a change no fingerprint can see is still caught within one poll. In between, a cheap fingerprint (registry records plus transcript mtimes/sizes - a handful of `stat()` calls) is probed every second, and any change triggers an immediate full refresh. This reacts within about a second while staying nearly free when nothing happens. A filesystem watcher was deliberately not used: transcript appends during generation would fire event storms, watcher buffers can overflow and drop events, and some changes produce no transcript file event at all - a process exiting, or a background child process starting or finishing - so a periodic full poll is needed either way.

## Subagents

Subagents (from the `Agent` tool and from workflows) do not run as separate OS processes - they run inside the Claude Code process, so a child-process count would not see them. Instead, Claude Code writes each subagent's transcript under `projects/<project>/<session>/subagents/` (workflow agents nested under `workflows/<wf>/`), each with a small `meta.json` (`agentType`, `description`, `toolUseId`).

Agent Monitor counts, per session, the subagents whose transcript was written very recently (running) and those that finished within the last few minutes - so a burst of 20 parallel agents shows how many are still running. The running badge's tooltip lists what each running subagent is doing, taken from its `meta.json` `description`. Only timestamps and those two `meta.json` fields are read; the subagents' own transcripts are never opened. Progress *within* a subagent is not shown - nothing records it.

## Jumping to a session

Clicking a session activates the window it runs in: the live ancestor chain supplies the host processes, their visible top-level windows are enumerated, and for hosts that keep several windows in one process (VS Code, JetBrains IDEs) the window whose title mentions the session's project folder is preferred. This works for any host reachable through the process chain, even ones the label table does not know.

For sessions of the VS Code extension the jump is **tab-exact**: after raising the right window, the extension's official deep link (`vscode://anthropic.claude-code/open?session=<id>`, available since extension v2.1.72) focuses the session's tab. The window is raised first because VS Code routes the deep link to the currently focused window. Session ids are strictly validated as UUIDs before the URI is launched.

Limitations: CLI-driven sessions get window-level jumps. Windows Terminal does offer `wt -w <window> focus-tab -t <index>`, but exposes no way to enumerate windows or tabs externally - and targeting a non-existent window silently creates a new one - so it is deliberately not used.

## A note on coupling

The session registry, the project-slug scheme, and the transcript schema are undocumented Claude Code internals and can change between versions. Agent Monitor parses them defensively - a missing or renamed field yields an `unknown` status or a skipped session rather than an error. If a Claude Code update ever changes the layout, the tool degrades gracefully instead of crashing.
