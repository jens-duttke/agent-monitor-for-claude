# How it works

Agent Monitor derives everything it shows from local files that Claude Code already writes, plus the operating-system process list.

## Data sources

Under the Claude config directory (`CLAUDE_CONFIG_DIR`, or `~/.claude/` by default):

- **Session registry** - `sessions/*.json`. Each running Claude Code process writes one file containing its PID, session ID, working directory, name, kind, start time (`startedAt`), and original process start time (`procStart`). This is the same set of records the `claude agents --json` command reports; Agent Monitor reads the files directly and never spawns the CLI. `procStart` is checked against the live process to unmask registry entries whose PID Windows has recycled for an unrelated process; `startedAt` provides the displayed age for fresh sessions that have no transcript yet. A session ID exists from the moment a window opens - the transcript file only appears with the first prompt, which is why a "New" session is not stale even though `claude --resume` reports no conversation for it.
- **Transcripts** - `projects/<project-slug>/<session-id>.jsonl`. The project slug is the working directory with every character that is not a letter or digit - the drive colon, path separators, dots, and any other punctuation - replaced by a single hyphen (e.g. `d:\WebDev\oku3d-app` becomes `d--WebDev-oku3d-app`, and `d:\WebDev\HexEd.it` becomes `d--WebDev-HexEd-it`).
- **Process list** - one `psutil` scan of the process table per snapshot tells, for every session at once: whether its process is still alive, whether it currently has a child process (a tool actually executing), and which application hosts it **right now** (ancestor chain). A shell between the session process and its GUI host marks the session as CLI-driven - so a conversation resumed with `claude --resume` in a terminal is labeled by where it runs at this moment, not by where it was originally started.

The live overview above is built entirely from the session registry, so it only ever shows sessions that still have a registry record. The **History** chip (off by default) adds the past sessions that no longer do: enabling it triggers a one-time, on-demand scan of the transcripts under `projects/`, skipping any session already shown live. Each past transcript is read once to resolve its title exactly as Claude Code shows it (a rename can sit anywhere in the file), its working directory (to group it under its project) and its last model and activity time; token usage is deliberately not summed for history entries, which roughly halves that read. The scan runs on a worker thread, so the window never blocks, and the result is cached, so it is paid only the first time you enable the chip.

## WSL sessions

Everything above works the same way for a Claude Code session running natively inside a WSL distribution - a `claude` process whose own working directory and `.claude` configuration live inside the distribution's own Linux filesystem, not merely a Windows session whose Bash tool happens to invoke WSL for a command. Status, cost, subagents, history, search, and deletion all work identically; only where the files are read from differs, and only while the `wsl` setting (see [configuration.md](configuration.md)) is on - it is, by default, and turning it off also stops the periodic reads below from being able to keep a watched distribution from idling out.

Windows is one **session root**; a second is added for every currently *running* WSL distribution that has a `.claude` directory - `home/<user>/.claude` under every user account, plus `root/.claude` for the root account, each its own root, read over the same `\\wsl.localhost\<distro>\...` path Windows exposes for a distribution that is already running. The first root found for a distribution keeps the plain origin `wsl:<distro>`; a second Claude-configured account on the same distribution (unusual, but possible) gets `wsl:<distro>:<home>`, so the common single-account case stays simple. Every session record carries this `origin` (`'windows'` or `'wsl:<distro>'`) and `origin_label` (the distribution's name; `None` on Windows); the UI uses it to label the row's host - a WSL session shows its distribution name suffixed "(WSL)" (e.g. "Ubuntu (WSL)") instead of a detected editor or terminal - and every action taken on a session (deleting it, searching it, opening its folder, focusing its window) carries the same `origin` back so it always resolves against the right root, never a different one.

Opening a path under `\\wsl.localhost\<distro>\` starts that distribution if it is not already running, so Agent Monitor never looks at one that is not already in the output of `wsl.exe --list --running --quiet` - the one program it ever runs, with fixed arguments, a hidden window, and a five-second timeout. Two gates keep this near-free while WSL is not in use: a dedicated process-table pass - separate from the per-snapshot scan described above, and cached for five seconds - checks for a shared `vmmem*` process (the WSL2 utility VM); without it, no distribution can possibly be running, so `wsl.exe` is never invoked at all. Once `vmmem` is seen, the running-distribution list itself is cached 10 seconds, so a poll every few seconds does not re-run `wsl.exe` every time - and the moment a check finds `vmmem` gone, both caches drop immediately, so a VM shutdown is noticed on the first check afterward - at most five seconds later - rather than lingering for the ten-second discovery window.

One limitation is accepted rather than engineered away: if one distribution is stopped while another keeps the shared VM alive, a poll landing inside that 10-second cache window can still show the stopped one as running and read it once - restarting it. It settles back down on its own: once it drops out of the `--running` list, nothing here keeps touching it, so it idles itself out again shortly after, same as if Agent Monitor did not exist.

A WSL session's registry record carries the same `procStart` field a Windows one does, but it means something else there: it is exactly field 22 of `/proc/<pid>/stat` (`starttime`, ticks since boot), read directly over the same UNC mount - no subprocess involved. A pid missing from `/proc` is not alive; a pid present whose `stat` field 22 does not match the registry's `procStart` was recycled by Linux for an unrelated process, so it reads not alive too - the same recycled-pid guard the Windows probe applies, adapted to procfs's own field.

Opening the process panel for a WSL session reads the same procfs mount further, on demand: each descendant's memory size and accumulated CPU time, sampled from the same `stat` entries the liveness probe already reads. Procfs has no instantaneous CPU figure, only a running tick count, so CPU is sampled the same way as a Windows process - against the previous reading, with the first sample reading as unknown. Converting those ticks, and the RSS page count, into seconds and bytes assumes the kernel's default 100 Hz clock tick and 4 KiB page size rather than querying either value, since querying would mean running a program inside the distribution; a distro with different values only skews the displayed CPU and uptime figures, never liveness, which compares raw `starttime` values directly.

The background-task output console reads from that root's own temp directory rather than the Windows one - a WSL root's `temp_dir` is the distribution's own `/tmp`, so a session's task-output files and scratchpad live under `\\wsl.localhost\<distro>\tmp\claude\...`. The same redirect-confinement rule applies: when a task's own output file is empty, its redirect target is followed only inside that session's own scratchpad or project directory, with the target translated to its Windows-readable form first - a `/mnt/<drive>/...` mount, or any other absolute path inside the distribution.

Search, history, and deletion all resolve a session's files against *that session's own root* - never a different one. A working directory or session id is confined the same way it is on Windows (the resolved path must sit inside that root's own `projects/` directory), so a crafted value can reach neither another root's files nor anywhere outside `projects/`. An origin naming a distribution that is no longer running resolves to nothing and is refused outright, never silently redirected to another root.

## Deleting a past session

From the row menu of a history entry - and only a history entry - you can permanently delete that session, after a confirmation. This removes its transcript (`projects/<slug>/<session-id>.jsonl`) and its subagent folder (`projects/<slug>/<session-id>/`) from disk, which also drops it from Claude Code's `--resume` list. It is the only action in the whole tool that writes anything to your Claude files; everything else is strictly read-only.

It is guarded so it can never harm a live conversation: the session id is validated as a UUID, the target paths are confined to `projects/`, and - immediately before deleting - the registry is re-checked so a session that has a live process is refused outright. A running session's files are therefore never touched.

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
