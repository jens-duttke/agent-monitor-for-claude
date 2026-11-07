# Privacy Policy

**Agent Monitor for Claude** shows the Claude Code sessions running on your machine. To do that it
reads Claude Code's local files, including your conversation transcripts. This page lists what it
reads, shows, runs and writes. It applies to Windows and Linux; differences are noted.

Last reviewed: 2026-11-07

## Summary

| | |
| --- | --- |
| Internet connections | None. Links (changelog, pull requests, branches, artifacts) open in your browser, only when you click them. |
| Credentials | Never read. |
| Telemetry, analytics, crash reports | None. |
| Data sent anywhere | Nothing. |
| Programs it runs | `git` (read-only) and, on Windows, `wsl.exe --list --running --quiet`. |
| Writes to disk | Its own interface preferences and settings, a session deletion you confirm, and a temp folder (Windows) or lock file (Linux). |
| Changes to your Claude Code data | Only the session deletion you confirm. |

## Network

The application has no HTTP client and opens no connections. Its only socket is the local server
(`127.0.0.1`) that delivers the interface to its own window. Before starting it, the app binds a port
briefly to check that it is free.

The page declares a Content-Security-Policy with `connect-src 'none'`, so the browser engine refuses
any network request from the interface. Images are allowed only from the app itself or as `data:`
addresses, which is how screenshots from a transcript are shown.

Links in the window are opened by your default browser, never by the app:

- **Changelog** - the Claude Code version links to its section of Claude Code's public changelog.
  The version is part of the `#` fragment, which your browser does not send to the server.
- **Pull requests and artifacts** - addresses Claude Code recorded in the transcript. They are links
  only if they are plain `https://` addresses without a query, fragment or user name.
- **Branches** - a link to the branch on GitHub, GitLab, Bitbucket or Gitea/Forgejo, built from the
  repository's remote address. User names, passwords and tokens in that address are removed.

The interface is rendered by Microsoft Edge WebView2 (Windows) or WebKitGTK (Linux). These engines
have their own network features (for example Safe Browsing on WebView2), which are governed by their
own settings, not by this app.

## Credentials

The app never opens `~/.claude/.credentials.json` and never reads an API key, token or cookie.

It opens Claude Code's `settings.json` to read one value: the default `effortLevel`.

## What it reads

All reads are local and read-only. The paths below are under `~/.claude/` (or `CLAUDE_CONFIG_DIR`).

| Source | What is used |
| --- | --- |
| `sessions/*.json` | Session id, process id, working directory, name, kind, status, start times, Claude Code version, whether Remote Control is on, a background agent's job id. The session's messaging address and former names are used only to match messages between sessions and are not shown. |
| `projects/<project>/<session>.jsonl` (transcripts) | Control data: entry types, stop reasons, tool names, timestamps, model, token usage, permission mode, git branch. Plus the display texts listed below. |
| `projects/<project>/<session>/subagents/` | Each subagent's type, task description, token usage and status. |
| `cache/model-catalog/*.json` | Each model's id and context window size. |
| `.last-update-result.json` | Whether Claude Code's last self-update succeeded, and its version. |
| `settings.json` | The default effort level. |
| Process table (`/proc` on Linux) | Process ids, parent ids, names and start times, to tell live sessions from ended ones. CPU and memory for the selected session's processes. |
| Window titles | Compared in memory to find the window to raise when you click a session. Not stored. |
| `agent-monitor-settings.json` | The app's own settings (see [What it writes](#what-it-writes)). An older copy in `~/.claude/` is still read. |

### Conversation text shown in the window

The transcripts contain your conversations. The app shows these parts of them:

- the session title (your own title, Claude Code's generated title, or the first prompt, cut to 80 characters)
- what a session runs or waits on: a command, a file path, a question with its answers, or a plan
- the beginning of its last answer
- its plan steps, subagent task descriptions, background-task labels and artifact titles
- the first line of an API error, a retried request's error summary, or a failed hook's error output
- who sent messages to whom between sessions and subagents (not the message text)

For the **selected** session or subagent, the details additionally show the full command and last
answer, its recent tool calls with their inputs and results, a subagent's task and result, the images
in its transcript, and its failed hooks with their command and output.

Other conversation text is shown only as search excerpts (see below).

### On-demand reads

These run only when you use the feature:

- **Search** - reads the transcripts of the sessions in the open list sections and shows up to five
  excerpts per session, about 60 characters either side of each hit. It reads more sessions only when
  you click to widen an empty result. To keep results up to date, it remembers in memory how far it
  read each transcript for the current query; clearing the search drops this. Your query is not saved.
- **Background-task output** - shows a task's console output while you have its row expanded. If the
  output was redirected to a file, that file is read only if it lies in the session's scratchpad or
  project folder. The last 16 KB of each task's output are also read to show test results.
- **Process details** - when you select a background process, its command line, program, working
  directory and start time are read. This works only for processes started by a listed session. A
  command line can contain secrets passed to the program; it is only shown in the window.
- **Older sessions** - opening the *Older* section reads the transcripts of past sessions within the
  selected time window (24 hours by default).
- **Git** - see [Programs it runs](#programs-it-runs).

### WSL (Windows only)

If the `wsl` setting is on (the default), the app also reads the Claude Code files and the process
table of each **running** WSL distribution over `\\wsl.localhost\<distro>\`. Stopped distributions are
not accessed, so they are not started. These reads count as activity and can keep a distribution from
shutting down on its own. Turn the setting off to stop them (see [docs/configuration.md](docs/configuration.md)).

## Programs it runs

**`git`** (Windows and Linux), to show whether a session's work is committed, pushed and merged, and
which files are uncommitted:

- read-only commands with fixed arguments (`status`, `diff`, `rev-parse`, `rev-list`, `for-each-ref`,
  `ls-files`, `remote get-url`), run in the session's directory
- never contacts a remote; remote state is as of your last `git fetch`
- found on `PATH` only, never in the current directory
- does not write the index (`GIT_OPTIONAL_LOCKS=0`), never prompts, and runs no external diff tool
- If git is missing or the folder is not a repository, the git information is not shown.

**`wsl.exe --list --running --quiet`** (Windows only), to find running WSL distributions. It is called
by its absolute `System32` path, with fixed arguments, and only if a WSL virtual machine is running.
Nothing is ever run inside a distribution.

## What it writes

1. **Interface preferences** - in `%LOCALAPPDATA%\AgentMonitorForClaude` (Windows) or
   `~/.local/share/agent-monitor-for-claude` (Linux). This is the browser engine's profile; the app
   stores open sections, time ranges, grouping, list width, search options, view settings, and which
   projects (by folder path) and graph sessions (by session id) you collapsed. The rest of the folder is
   the engine's own cache. You can delete the folder at any time; the preferences then reset.
2. **Settings** - `agent-monitor-settings.json` in `%APPDATA%\AgentMonitorForClaude` (Windows) or
   `~/.config/agent-monitor-for-claude` (Linux), or next to the executable if the file is already
   there. Written only when you save in the settings dialog. It holds only the app's own settings.
3. **Session deletion** - deletes a past session's transcript and subagent folder, only after you
   confirm. The app refuses if the session is still running, and only deletes files inside `projects/`.
4. **Temp folder (Windows executable)** - the single-file executable unpacks itself to
   `%TEMP%\_MEIxxxxxx` at start and removes it on exit. If the app is killed, the folder stays and can be
   deleted.
5. **Lock file (Linux)** - `$XDG_RUNTIME_DIR/agent-monitor-for-claude.lock`, holding the app's process
   id and version, so only one window runs. Windows uses a named mutex and shared memory instead, which
   are not files.

It also writes to the clipboard when you copy something. It writes no log files and never modifies
your transcripts, registry or Claude Code settings.

On Linux, `linux/install-desktop-entry` adds a desktop entry and icon under `~/.local/share` - only when
you run it yourself. `--remove` deletes them again.

## Actions on your behalf

Only when you click or press a key:

- **Raise a window** - brings a session's window to the front. On Windows this may simulate one Alt key
  press, which Windows requires for the switch. No keyboard input is recorded.
- **Open a VS Code session** - launches `vscode://anthropic.claude-code/open?session=<id>`.
- **Open a folder or show a file** - opens the project folder or scratchpad, or shows the transcript in
  your file manager. Files are never opened.
- **Copy** - copies the session id, a background agent's `claude attach` command, or selected text to
  the clipboard.
- **Taskbar** - shows how many sessions wait for you on the app's taskbar button (Windows), or asks
  for attention (Linux).
- **Replace a running instance** - if you start the app twice and confirm, the first instance is closed.

## Third-party services

None. Prices come from the local `pricing.json`. Dependencies: `pywebview`, `psutil` and `msgspec`,
plus the browser engine (WebView2 or WebKitGTK) and, on Linux, PyGObject.

## Verify it yourself

In a clone of the repository:

```sh
# Network clients - one hit: app.py: import socket (the free-port check)
grep -rnE "^\s*(import|from)\s+(requests|urllib|http|socket|ssl)\b" agent_monitor_for_claude/

# Remote addresses - three hits: the changelog link in ui/logic.js, the https:// check for
# artifact addresses in transcript.py, and the branch link built in git_overview.py
grep -rn "https\?://" agent_monitor_for_claude/ --include=*.py --include=*.js --include=*.html --include=*.css

# Credentials - one hit: a docstring saying none are read
grep -rni "credential" agent_monitor_for_claude/ --exclude-dir=__pycache__

# Disk writes - the settings file (settings.py), the session deletion (session_delete.py), the Linux
# lock file (platforms/instance_linux.py) and the console for --verbose (platforms/win32.py, CONOUT$)
grep -rnE "write_text|write_bytes|open\([^)]*['\"][wax]|os\.open|os\.write|os\.ftruncate|\.unlink\(|shutil\.rmtree|os\.remove|\.mkdir\(" agent_monitor_for_claude/ --exclude-dir=__pycache__

# Dynamic code - only regular expressions' .exec() calls and two comments
grep -rnE "\b(eval|exec)\s*\(|__import__|b64decode" agent_monitor_for_claude/ --exclude-dir=__pycache__

# Started programs - two hits: git_status.py and wsl.py
grep -rnE "subprocess\.[A-Za-z_]*\(|os\.system|os\.popen|Popen\(|ShellExecute|CreateProcess|os\.exec" agent_monitor_for_claude/ --include=*.py

# The Content-Security-Policy
grep -n -A 3 "Content-Security-Policy" agent_monitor_for_claude/ui/index.html
```

The policy contains `'unsafe-eval'` because pywebview's bridge between the window and Python needs it
on Linux. The app's own code does not use `eval` or `new Function`.

The tests check these boundaries and run without network access:

```sh
python -m unittest discover -s tests
node --test tests/js/logic.test.js tests/js/global-scope.test.js
```

The most relevant ones are `tests/test_transcript_privacy.py` (no conversation text beyond the listed
display fields), `tests/test_search.py` (excerpt limits, reads stay inside `projects/`),
`tests/test_session_delete.py` (deletion guards), `tests/test_tasks.py` (task-output confinement),
`tests/test_wsl.py` (stopped distributions are never touched) and the "HTML safety" tests in
`tests/js/logic.test.js`.

If you find a statement here that the code does not support, please report it as a bug.

## Contact

Please open an issue at https://github.com/jens-duttke/agent-monitor-for-claude/issues
