# How it works

Agent Monitor reads the files Claude Code writes locally and the operating system's process list. It never talks to Claude Code or the network.

## Data sources

Under the Claude config directory (`CLAUDE_CONFIG_DIR`, or `~/.claude/`):

- **Session registry** - `sessions/*.json`. Every running Claude Code process writes one file with its process id, session id, working directory, start time, status and version. The app checks each record against the live process, so a record left behind by an ended process is not shown as running.
- **Transcripts** - `projects/<project-slug>/<session-id>.jsonl`. The slug is the session's launch directory with every character that is not a letter or digit replaced by `-` (`d:\WebDev\my.app` becomes `d--WebDev-my-app`). A session that switched into a git worktree keeps its transcript under the launch directory; it is listed under that project, with the worktree shown in its details.
- **Subagents** - `projects/<project-slug>/<session-id>/subagents/`, one transcript and one `meta.json` per subagent.
- **Model catalog** - `cache/model-catalog/*.json`, for each model's context window size. Without it, the context is shown as a token count only.
- **Last self-update** - `.last-update-result.json`, to tell when a restart would bring a newer Claude Code.
- **Process list** - shows whether a session's process is alive, whether it has child processes (a tool running), and which application hosts it.

Each transcript is read in full once, then only the lines appended since. Large transcripts are read in the background, so the window shows every session right away; until the read is done, the cost shows `…`. The selected session is read first.

The *Older* section reads past sessions under `projects/` only when you open it, and only within the selected time window.

## Status

The status comes from the newest transcript entry, not from how long ago it was written. A model can think for minutes without writing anything, so a quiet session is not treated as finished.

The first matching rule wins:

| Newest entry / signal | Status |
|-----------------------|--------|
| the process has exited | **Finished** |
| no transcript yet | **New** |
| the interrupt marker `[Request interrupted by user]` | **Interrupted** |
| an API error | **Error**, with the cause where known (*Usage limit reached*, *servers overloaded*, *authentication failed*, *HTTP nnn*). A usage limit counts down to its reset. |
| the newest turn is older than the process holding the session (the session was reopened) | **Idle** |
| a pending question or plan dialog | **Question for you** / **Plan review** |
| a pending tool with a child process, subagent or workflow running | **Working** |
| a pending tool in *Manual* mode, or a pending non-edit tool in *Auto-edit* mode | **Permission needed** |
| any other pending tool, unchanged for 5 minutes (90 seconds in *Auto* mode) | **Waiting for you** |
| any other pending tool | **Working** |
| a finished turn that Claude Desktop marked as blocked | **Blocked** |
| a finished turn, or a local command such as `/compact` | **Idle** (*Finished with a question* if Claude Desktop's turn asked you something) |
| a prompt, a tool result, or an unfinished reply | **Working** (*Retrying (2/10)* while Claude Code retries a request) |
| anything else | **Unknown** |

Further rules:

- If a turn has finished but a subagent, a workflow or a child process is still running, the session reads **Background**.
- A `/loop` waiting for its next run reads **Wakes in 4m**.
- If the registry carries Claude Code's own status (`busy`, `idle`, `waiting`), it refines the result. It never overrides an interrupt, an error or a detected permission prompt.
- In *Auto* mode, three refused tool calls in a row put Claude Code's classifier on hold; the session's mode is then marked as paused.
- Subagent entries inside the main transcript do not affect the status.

The time since the last activity comes from the newest entry's timestamp, not from the file's modification time.

The app checks every second whether any registry file or transcript changed, and refreshes at once if so. A full refresh also runs every `poll_interval` seconds (default 5).

## Subagents

Subagents run inside the Claude Code process, not as separate processes. Agent Monitor counts them from their transcript files: one that was written recently and has not finished is running. A workflow's progress comes from its `journal.jsonl`. A session's cost includes all its subagents.

## Jumping to a session

A click raises the window the session runs in, found through its process's parent chain. For editors with several windows in one process (VS Code, JetBrains), the window whose title names the project folder is chosen. For the VS Code extension, the exact session tab is then focused through the extension's deep link `vscode://anthropic.claude-code/open?session=<id>`.

On Windows, sessions in a terminal are matched to a terminal window by title. Individual terminal tabs cannot be selected.

On Linux the window list comes from the X server. Windows of native Wayland apps cannot be raised.

A background agent (`claude --bg`) has no window. Its details offer the `claude attach <id>` command instead.

## WSL sessions

On Windows, Claude Code sessions running inside WSL distributions are listed too, with the distribution as their host. The app reads their files over `\\wsl.localhost\<distro>\` - only for distributions listed by `wsl.exe --list --running --quiet`, so a stopped distribution is never started. That command runs only while a WSL virtual machine is running.

The `wsl` setting turns this off (see [configuration.md](configuration.md)).

## Git

For the selected session, the app runs `git status` and `git diff` in its working directory every five seconds to list uncommitted files. For every listed session, it checks in the background whether the branch is pushed, local-only or deleted on the remote, using the state of your last `git fetch`. Git never contacts the remote.

## Test results

The last 16 KB of each background task's output are checked for a test runner's summary (pytest, unittest, Jest, Vitest, Node's test runner, TAP, cargo, go test). The newest summary is shown.

## Deleting a past session

From the details or menu of an *Older* entry, you can delete that session's transcript and subagent folder after a confirmation. It then also disappears from `claude --resume`. A session that is still running cannot be deleted.

## Compatibility

The session registry and transcript format are undocumented Claude Code internals and can change with any version. Agent Monitor reads them defensively: an unknown or missing field leads to an *Unknown* status or a skipped session, not a crash.
