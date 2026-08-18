# Privacy Policy

**Agent Monitor for Claude** is a local desktop application that shows the state of the Claude Code
sessions running on your own machine. To do that it reads Claude Code's local session files, which
include your conversation transcripts. This document states exactly what it reads, what it keeps, what
it shows, what it writes, and what it never does.

Last reviewed: 2026-08-12

## At a glance

| Question | Answer |
| --- | --- |
| Does the application connect to the internet? | No. It has no HTTP client and no network client import in any of its own modules. It carries exactly one remote address - a link to Claude Code's public changelog - which it never requests itself; clicking it opens your normal browser. See [Network Communication](#network-communication). The embedded Microsoft browser engine it renders in is a separate matter, stated there too. |
| Does it run any other program? | Yes, exactly one, and only to list which WSL distributions are currently running: `wsl.exe --list --running --quiet`, invoked by its absolute `System32` path so no same-named file elsewhere can ever be run in its place. Nothing is ever run *inside* a distribution. See [Programs it runs](#programs-it-runs). |
| Does it read your credentials? | No. It never opens `.credentials.json` and never reads a token, key, or cookie. |
| Does it send telemetry, analytics, or crash reports? | No. None, of any kind. |
| Does it send your data anywhere? | No. It never transmits anything it reads, and the page it renders declares a Content-Security-Policy that forbids network requests outright, so the browser engine would refuse one even if the code asked. |
| Does it read your conversations? | It scans transcript files for control metadata, and takes three short display fields out of them. Two on-demand features go further: the content search, and the background-task console. |
| Does it display conversation text? | Three short fields: the session title, each subagent's task description, and a background task's label. Nothing else from a conversation is ever shown. |
| Does it write to disk? | Only its own files, in three places: the browser profile that stores your interface preferences, a session deletion you explicitly confirm, and its own program bundle unpacked into your temp folder at startup. |
| Does it modify your Claude Code data? | Never. The only removal is the session deletion you confirm yourself. |

## Data Collection

The application collects nothing. It has no account, no identifier, no usage counter, and no
reporting of any kind. Everything it derives from your session data exists only in the memory of the
running process and is gone when you close the window. The one thing it keeps between runs is your
own interface preferences, described under [What the application
writes](#what-the-application-writes).

## Network Communication

The **application** makes no outbound network connections. Its own code contains no HTTP client: no
`requests`, no `urllib`, no `socket`, no `http.client` import anywhere in its modules. There is no code
path that could transmit anything it reads.

Its code does contain exactly **one** remote address, and it is worth being precise about what that
address does and does not mean:

- **The changelog link.** Each session's row can show which Claude Code version wrote it, and that
  version number is a link to its section of Claude Code's public changelog on GitHub
  (`https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md`, the only URL in the code). The
  application never requests it. Clicking it hands the address to Windows, which opens it in your
  normal browser, in a new tab of its own - the same thing that happens when you click a link in any
  document. Nothing about your sessions travels with it: the address is a fixed page plus a `#`
  fragment naming the version, and a fragment is resolved by the browser and never sent to the server,
  so not even which version you looked up leaves your machine. If you never click it, nothing is ever
  contacted.

Two further things about the running process are worth stating plainly, because anyone inspecting it
will find them:

- **A loopback HTTP server.** The user interface is a bundled set of local HTML, CSS, and JavaScript
  files. pywebview serves those files to the application's own window over a small HTTP server bound
  to the loopback interface (`127.0.0.1`) on a fixed port, so nothing on your network can reach it.
  The fixed port keeps the browser origin stable across restarts, which is what lets the interface
  remember your preferences. Its document root is the interface folder; pywebview additionally
  registers one route of its own for a JavaScript bridge, which goes unused on Windows because the
  WebView2 host provides a native bridge instead. This is the only socket the application itself
  opens, and it stops when you close the window.
- **An embedded browser engine.** The interface is rendered by the Microsoft Edge WebView2 runtime
  that Windows ships. It is a full Chromium engine, and it brings its own machinery with it: its
  profile folder shows the usual Chromium components for Safe Browsing, SmartScreen, certificate
  revocation lists, field trials, and crash reporting, each with its own network behaviour. Those
  belong to Microsoft's runtime, are governed by Microsoft's terms, and behave the same in any
  WebView2 application on your system. What this application controls is what it puts *into* that
  engine: only the local interface files, plus the session data the window displays, handed over
  WebView2's in-process bridge. It never navigates the engine to a remote address, and nothing it
  reads is ever placed into a network request - the interface code contains no `fetch`,
  `XMLHttpRequest`, `WebSocket`, or remote resource reference at all. That is not left to good
  behaviour: the page declares a **Content-Security-Policy** that forbids it structurally -
  `default-src 'none'` with `connect-src 'none'`, so `fetch`, `XMLHttpRequest`, `WebSocket`, and
  `EventSource` have nowhere to go, and scripts, styles, and images may come from this page's own
  origin only. A remote address in the interface would be refused by the engine, not merely absent
  from the code. If you want the runtime's own machinery off, WebView2 is a Windows component and is
  configured at the Windows level, not by this application.

## Credentials

The application reads **no credentials of any kind**. It never opens
`~/.claude/.credentials.json`, never reads an API key, OAuth token, or session cookie, and never
authenticates against anything - it has nothing to authenticate against.

One file that can hold sensitive settings is opened: Claude Code's own `settings.json` in the config
directory. To be precise about what that means - the file is parsed as JSON in memory, as any reader
must - exactly one value is then taken out of it, the global `effortLevel`, so the window can show
which effort level your sessions default to. Nothing else from that file is extracted, kept, or
displayed.

## Programs it runs

Everything described so far is about reading files and probing the process table - not running other
software. Exactly one external program is ever run, and only when a WSL distribution might be involved:

`wsl.exe --list --running --quiet` - Windows' own command, invoked by its absolute path
(`%SystemRoot%\System32\wsl.exe`, so nothing planted next to the application or in its working
directory can stand in for it) for listing which WSL distributions are
currently running right now. Always these three fixed arguments and nothing else: no text you typed and
nothing from your session data is ever placed on its command line. It runs in a hidden window with a
five-second timeout; only its output - a list of distribution names - is read, and nothing else happens
with it. This is enumeration only: no code is ever executed *inside* a WSL distribution, by this
application or through it, and it is the only external program the application ever runs. This differs
from the shell actions under [Actions performed on your behalf](#actions-performed-on-your-behalf) -
those hand a path or a URI to Windows and let it decide what opens; this is the one place the
application itself starts a process, waits for it, and reads what it printed.

It runs rarely, not on every refresh. A dedicated pass over the process table - separate from the one
[Processes and windows](#processes-and-windows) describes, and cached for five seconds - checks for a
`vmmem*` process, the shared WSL2 utility VM, and `wsl.exe` is skipped entirely when that is absent,
which it is on any machine with no WSL distribution currently running. When `vmmem` is present, the
distribution list is still cached for about ten seconds, so a normal poll cadence does not re-run it
every cycle. Turn the `wsl` setting off (see [docs/configuration.md](docs/configuration.md)) and none of
this - the extra process check, the cache, or the command itself - ever runs.

**A stopped distribution is never touched.** Reading a file under `\\wsl.localhost\<distro>\` starts
that distribution if it was not already running, so every WSL read described below only ever targets a
distribution this command just reported as running; one absent from that list is never opened, listed,
or globbed into - see [Sessions running inside WSL](#sessions-running-inside-wsl). One narrow, documented
exception: if a distribution is stopped while another keeps the shared WSL2 VM alive, a check that lands
inside that ten-second cache window can still show the stopped one as running and read it once, which
restarts it. It settles back down on its own: nothing here keeps re-reading a distribution once it drops
off the running list, so it idles itself out again shortly after - the same as if this application did
not exist.

## What the application reads

All reads are local, and all of them are read-only. Two different things are worth telling apart, and
this section is explicit about both:

- **Scanning** a file means its bytes pass through a parser. Several features scan whole transcripts.
- **Taking** a field means a value is kept, used, or shown. That is the far shorter list, and it is
  what determines what you can actually see in the window.

### The session registry

`~/.claude/sessions/*.json` (or `$CLAUDE_CONFIG_DIR/sessions/`) - the records Claude Code writes for
each running session. Fields taken: session id, process id, working directory, project name, session
kind and entry point, the native status and its waiting reason, and two timestamps. No conversation
data is in these files.

### Transcripts, for status metadata

`~/.claude/projects/<project>/<session>.jsonl` - your conversation transcripts. To answer "what is
this session doing right now", the application scans each transcript: the whole file once when it
first sees it, and from then on only what has been appended since. Every line passes through its
parser.

What it **takes** from that scan is control-flow metadata: entry types, stop reasons, tool ids and
tool names, timestamps, the model id, the Claude Code version stamped on each entry, token-usage
numbers, the permission mode, and the flags that distinguish a real turn from a sidechain, an injected
notice, or an API error. Message text, thinking
blocks, tool inputs, and tool results are not taken, with these deliberate exceptions - three that are
displayed to you, and two that are reduced to a flag or an id and never shown:

- **The session title** (displayed). In order of preference: a title you set yourself, the title
  Claude Code generated for its own session list, or - if neither exists - your **first prompt** in
  that session, stripped of editor wrapper blocks and truncated to 80 characters. This is
  conversation text, it is the row label you see in the window, and it is exactly what Claude Code
  shows you in its own session list. Only prompts are considered, and only until one yields
  displayable text: an entry that holds tool output, or nothing but wrapper blocks, is skipped in
  favour of the next one.
- **Subagent task descriptions** (displayed). The one-line description of what each running subagent
  was asked to do, so the subagent badge can say what is in flight. Read from each subagent's own
  `agent-<id>.meta.json` file, not from its messages.
- **Background-task labels** (displayed). The `description` (or, failing that, the command line) of a
  background shell command, so a task row can be named instead of showing an opaque id. This is a
  tool input.
- **The interrupt marker** (not displayed). To tell "you stopped this turn" apart from "you sent a
  new prompt", a user entry's text is prefix-matched against Claude Code's fixed marker
  `[Request interrupted by user`. Only the yes/no result is kept; the text itself is neither returned
  nor stored.
- **A background task's registration line** (not displayed). To map a task id to the command that
  started it, the tool result of a background shell call is searched for Claude Code's fixed
  "Output is being written to" line. Only the id mapping is kept.

Nothing from these files is transmitted anywhere, because there is nowhere for it to go.

### Subagent and workflow files

`~/.claude/projects/<project>/<session>/subagents/**` - to count how many subagents are running and
how many just finished. Per subagent this reads the last 64 KB of its `agent-*.jsonl` transcript and
parses only the final entry's control fields (entry type, stop reason, content-block type), plus that
subagent's `agent-<id>.meta.json` for its agent type and the task description named above. For a
background workflow, the run's `journal.jsonl` is scanned line by line, and only each event's type and
agent id are taken - which gives the run's agent count and whether it is still active. No subagent's
messages, reasoning, or returned results are read or shown.

### Transcripts, for the content search (on demand)

The search box narrows the list to sessions whose transcript **contains** what you type. It is the one
feature that matches conversation text against something you supplied, so it is deliberately confined:

- It runs only when there is a query you typed. While a search is active it also re-runs by itself
  when your session data changes, so a match that has only just appeared shows up without you
  retyping. With the search box empty, no transcript is ever opened for it.
- It reads only the transcripts of the sessions currently shown by your filter chips. A session
  hidden by a filter is not opened - the single exception being the widening in the next point, which
  only a click of yours can start. On a self-triggered refresh the scope is narrower still: only live
  sessions that have not already matched.
- That scope widens only when you click to widen it. When a search finds nothing, the empty result
  offers one button - first to include the sessions your chips hide, then to reach further back in
  time - and each click adds exactly that one step for exactly that one query. Nothing widens on its
  own. It narrows back the moment you edit the search text, or immediately when you use the "Reset"
  control on the line above the results.
- It answers one question per file - does this transcript contain the string - and abandons the file
  at the first hit.
- It reports back **only the ids of the matching sessions**. Not a line, not a snippet, not a
  character of matched text ever leaves the search code, reaches the interface, or is stored
  anywhere.
- Your query itself is not saved. The three search toggles (match case, whole word, regular
  expression) are remembered; the text you searched for is not.

### Background-task output (on demand)

Expanding a background task's row shows that task's live console output. This surfaces **process
output** - what a build, test run, or script printed - and never conversation content. It is read only
while you have that row expanded, never in the background, and only the tail of the file.

The file read is the output file Claude Code writes for the task, under the system temp directory. If
that file is empty because the command redirected its output elsewhere (`... > run.log 2>&1`), the
redirect target parsed from the recorded command is read instead - but only when that target resolves
inside the session's own scratchpad directory or its project directory. A redirect pointing anywhere
else on your disk is ignored.

That output is displayed as text, never as page markup: every run of characters is HTML-escaped before
it reaches the window, and the only thing derived from the terminal's colour codes is a class name from
a fixed internal list. Output cannot introduce an element, an attribute, or a script into the interface.

### Processes and windows

To tell a live session from a finished one and to count its background work, each refresh takes one
snapshot of the Windows process table. That snapshot is machine-wide by nature - it yields the process
id, parent process id, and executable name of every running process - and the application uses it to
walk the ancestry and descendants of the session processes. Nothing else about a process is read: no
command line, no environment, no open handles, no memory contents. For the sessions on screen it
additionally samples CPU, memory, and start time; when you open the process panel for a **Windows**
session whose tools run through the `wsl.exe`/`wslhost.exe` relay, the shared WSL virtual machine's CPU
and memory are sampled too, which the panel labels as machine-wide because that figure is not specific to
your session. (This is not the same as a session running natively inside a WSL distribution - see
[Sessions running inside WSL](#sessions-running-inside-wsl) below.)

When you click a session, the titles of all visible top-level windows are enumerated in memory to find
the right window to raise. Those titles are compared and discarded - never stored, logged, or shown.

### Configuration and appearance

Its own optional settings file (`agent-monitor-settings.json`, read-only), its bundled translation and
price files, and one registry value under `HKEY_CURRENT_USER` that reports whether Windows is in light
or dark mode.

Started with `--verbose`, it prints environment diagnostics to the console: this reads the registry
entries recording the installed WebView2 runtime (under both `HKEY_CURRENT_USER` and
`HKEY_LOCAL_MACHINE`) and the installed .NET Framework release, plus the version metadata of its own
Python packages. That console output also carries interface error messages, which can quote a session
title. It goes to the console you launched from, and nowhere else - unless you redirect it to a file
yourself.

### The history listing (on demand)

Enabling the *Older* filter lists finished sessions by scanning the `projects/` folder. Each past
transcript is read once in full, but only three things are taken from it: the title fields, the first
prompt (as the fallback title described above), and the session's working directory, which is needed
to group it under its project.

The listing reaches back only as far as the window selected on the chip itself (24 hours by default).
A transcript whose file has not been written within that window is skipped on its timestamp alone and
never opened, so with the default window the scan reads only the handful of sessions from the last day
- not every session you have ever run.

### Sessions running inside WSL

Everything above applies just the same to a Claude Code session whose own process runs inside a running
WSL distribution - the session registry, the transcripts (and every field taken from them, with the same
exceptions), the subagent and workflow files, the content search, and the history listing. Only the
location differs: instead of `~/.claude/`, it is read at `\\wsl.localhost\<distro>\home\<user>\.claude\`
(or `\...\root\.claude\` for the root account) - the same UNC path Windows itself exposes for a
distribution that is already running. This is optional and on by default; the `wsl` setting (see
[docs/configuration.md](docs/configuration.md)) turns it off, and [Programs it runs](#programs-it-runs)
states the guarantee that a distribution which is not already running is never touched at all.

One consequence of watching a distribution that *is* running is worth stating plainly: these periodic
reads count as activity against it and can keep it from idling out on its own; turning the `wsl` setting
off stops that too, along with everything else in this section.

One more thing is read that has no Windows counterpart: `\\wsl.localhost\<distro>\proc\`, the
distribution's own Linux process table, exposed read-only over the same mount. Every refresh it stands in
for the Windows process-table scan described under [Processes and windows](#processes-and-windows) -
process names and parent-process links, so a session's liveness and its running child processes can be
determined, plus each process's recorded start time, compared the same way `procStart` is on Windows.

Opening the process panel for a session running inside WSL reads that same mount further, on demand,
exactly as the panel does for a Windows session: the memory size and accumulated CPU time of each of the
session's own descendant processes, so the panel shows live CPU, memory, and uptime for a WSL session too.
Nothing beyond those figures is ever taken from it: no command line, no environment variable, no open file
handle, and no actual memory content - only the byte count and CPU-time figures the kernel reports for
each process. Because this is the session's own Linux process tree rather than a Windows-side relay, the
panel never adds the shared `vmmem*` row described above - these rows already are the session's real work,
not a stand-in for work happening elsewhere.

The background-task output console reads the same way for a WSL session, from the distribution's own
temporary directory instead of the Windows one -
`\\wsl.localhost\<distro>\tmp\claude\<project-slug>\<session-id>\tasks\<task-id>.output` - read only while
that task's row is expanded, exactly as described under [Background-task
output](#background-task-output-on-demand). A redirected task's output is followed under the identical
rule, confined to that session's own scratchpad or project directory; both are resolved against the
distribution's own filesystem, with a `/mnt/<drive>/...` path, or any other absolute path inside the
distribution, translated to its Windows-readable form before that confinement check runs.

## What the application writes

Three places on disk, all of them the application's own: two that the application itself writes, and
one that the launcher of the single-file build unpacks before any of its code runs. None of them holds
a copy of your conversations.

1. **Its interface-preference profile**, in `%LOCALAPPDATA%\AgentMonitorForClaude`. This is a WebView2
   browser profile, and the application uses it for one purpose: so the window can remember your
   choices in `localStorage`. The current version stores seven values there - theme, which filter
   chips are on, sort field and direction, whether priority ordering is on, your three search toggles,
   and which project panels you collapsed. The collapsed-panel entry stores project directory paths,
   since that is what identifies a panel; the others are short flags. (An older version's leftover key
   may still sit there unused.)

   Be aware that the rest of that folder is WebView2's own, and it looks like what it is - a browser
   profile. Like any Chromium profile it keeps HTTP, code, GPU, and shader caches, its own logs, and
   crash-report scaffolding there, and it also creates the empty database files a browser would use
   for saved passwords, browsing history, and cookies. Chromium creates those in every profile; this
   application never navigates to a website and never logs in anywhere, so there is nothing for them
   to record. All of it is written by Microsoft's runtime, not by this application, and none of it
   contains your session data. Deleting the folder is safe: WebView2 rebuilds it and your interface
   preferences start from their defaults.
2. **A session deletion you ask for.** A past session's row menu offers to permanently delete that
   session's transcript file and its subagent folder. It happens only on your click, after an
   in-application confirmation, and it is guarded three ways: the session id must be a well-formed
   UUID, both target paths must resolve inside the `projects/` folder, and the session registry is
   re-checked immediately before deleting so that a session with a live process is refused outright. A
   running conversation's files are never touched.
3. **Its own program bundle, unpacked at startup.** The released build is a single-file executable, so
   its launcher extracts the bundled Python runtime, libraries, and interface files into a temporary
   folder (`%TEMP%\_MEIxxxxxx`) before any of the application's own code runs. It contains only the
   application's own files, never your data, and is removed when the application exits normally. A hard
   termination leaves one behind, which is safe to delete - including when you use the application's own
   "replace the running instance" prompt, since that terminates the old process outright. Running from
   source does not do this.

Nothing else is written to disk, moved, or deleted. Your transcripts, session registry, and Claude
Code settings are never modified. The application writes no log file of its own and keeps no copy of
anything it reads. Two writes exist that do not touch your disk at all - the clipboard, and a small
named shared-memory block used to find an already-running window - and both are described in the next
section.

## Actions performed on your behalf

These are the ways the application reaches outside its own window. Each happens only in response to
something you did:

- **Raising a window.** Standard Windows calls bring a session's host window to the foreground. When
  Windows refuses the foreground change - it does so in some states - the documented workaround is
  used: a synthetic Alt key press and release, which lifts the restriction. That is the only input the
  application ever synthesizes. It installs no hook and reads no keyboard state, so no keystroke of
  yours is ever observed or recorded.
- **Focusing a session tab.** Launching the Claude Code VS Code extension's official deep link,
  `vscode://anthropic.claude-code/open?session=<uuid>`. The session id is validated as a UUID first,
  and this is the only URI the application itself launches.
- **Opening the changelog.** Clicking a version number in the CLI-version column follows an ordinary
  link, which the window host passes to your default browser (see [Network
  Communication](#network-communication)). The link is only built when the version reads as a plain
  release number - three groups of digits separated by dots - so the address can never hold anything
  but digits and dots, whatever a transcript claims its version is.
- **Opening a folder.** Opening a session's project folder or its scratchpad in Windows Explorer. The
  path is verified to be an existing directory before the shell sees it, so nothing else can be
  launched through it.
- **Copying a session id.** The row menu's copy action places the session id on your clipboard. It is
  the only thing ever copied there, and only when you ask for it. Like any clipboard write it replaces
  what was there before.
- **Replacing an already-running instance.** Only one window may run at a time. A second launch finds
  the first through a named mutex, and a named shared-memory block - holding this application's own
  process id and version, nothing else - identifies it. You are then asked, in a Yes/No dialog, whether
  to replace the running instance; only if you say yes is that process terminated. No other process is
  ever touched.

## Third-Party Services

The application integrates with **no** third-party service. No analytics, no tracking, no advertising,
no telemetry, no crash reporting, no update check, no remote configuration. Model prices are read from
a hand-maintained local `pricing.json` in the installation, never fetched from anywhere.

It has two direct Python dependencies, `pywebview` and `psutil`. Through pywebview it also uses
`bottle` (the loopback file server described above) and `pythonnet`/`clr_loader` (the .NET bridge to
the window host, which the application calls directly to keep the window background in step with your
theme). Rendering is done by the Microsoft Edge WebView2 runtime that Windows provides.

## Verify it yourself

These guarantees are meant to be checked, not taken on faith. In a clone of the repository:

```sh
# No network client anywhere in the application code
grep -rnE "^\s*(import|from)\s+(requests|urllib|http|socket|ssl)\b" agent_monitor_for_claude/

# Every remote address in the application code - interface files included, not
# just Python. Exactly one hit, the changelog link described above:
#   ui/logic.js: const CHANGELOG_URL = 'https://github.com/anthropics/claude-code/...'
grep -rn "https\?://" agent_monitor_for_claude/ --include=*.py --include=*.js --include=*.html --include=*.css

# No credential access
grep -rni "credential" agent_monitor_for_claude/ --exclude-dir=__pycache__

# Every disk write and delete in the application code
grep -rnE "write_text|open\([^)]*['\"][wax]|\.unlink\(|shutil\.rmtree|os\.remove" agent_monitor_for_claude/ --exclude-dir=__pycache__

# No dynamic code execution
grep -rnE "\b(eval|exec)\s*\(|__import__|b64decode" agent_monitor_for_claude/ --exclude-dir=__pycache__

# The page forbids itself from reaching the network
grep -n -A 3 "Content-Security-Policy" agent_monitor_for_claude/ui/index.html
```

The first two commands find nothing at all, and the last one prints the policy quoted above. The
remaining three find a handful of lines, and every one of them is accounted for here, so that nothing
looks like a hidden exception:

- The credential search matches two docstrings, both stating that no credentials are read.
- The write search matches the two deletion calls in
  [session_delete.py](agent_monitor_for_claude/session_delete.py) - the confirmed session deletion
  described above - and two `open('CONOUT$', 'w')` calls in
  [verbose.py](agent_monitor_for_claude/verbose.py). `CONOUT$` is the Windows console screen buffer,
  not a file: those two lines attach a console so `--verbose` diagnostics have somewhere to print.
  Nothing is created on disk. The other two disk surfaces do not appear in this search because the
  application code does not perform them: the preference folder is created and filled by pywebview and
  WebView2, which [app.py](agent_monitor_for_claude/app.py) points at that location, and the temp
  extraction is done by the single-file launcher before any application code runs.
- The dynamic-execution search matches one line in `ui/logic.js`, a regular expression's `.exec()`
  call used to strip terminal color codes. There is no `eval`, no `exec`, no `__import__`, and no
  encoded payload anywhere in the application. (`compile` does occur, as `re.compile`, which builds a
  regular expression and cannot execute code.) One related call deserves naming: the backend uses
  pywebview's `evaluate_js` in exactly one place, to hand the interface the list of matching session
  ids during a search. That one statement is a fixed function call whose only interpolated value is a
  `json.dumps` payload of session ids; no user-supplied text is ever placed into it.

One more guarantee is worth checking directly: that the one external program this application ever runs
is invoked from exactly one place, and that the module owning it never spawns a process any other way.

```sh
# Where wsl.exe is actually invoked, across the whole application
grep -rn "_WSL_EXE" agent_monitor_for_claude/ --include=*.py

# Proof that wsl.py itself never spawns a process any other way
grep -nE "subprocess\.[A-Za-z_]*\(|os\.system|os\.popen|Popen\(|ShellExecute|CreateProcess|os\.exec" agent_monitor_for_claude/wsl.py
```

The first command prints exactly two lines, both in `wsl.py`: line 55, where the absolute path is built
(`_WSL_EXE = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'wsl.exe')`), and
line 179, the `[_WSL_EXE, '--list', '--running', '--quiet']` argument list - the single place in the
entire codebase where that command line is used. The absolute path matters: a relative `wsl.exe` would
resolve through the Win32 process-creation search order, which checks the application's own directory
and the current directory before System32, so a same-named file planted next to the executable could
otherwise be run in its place. The second command prints exactly one line, `wsl.py:178`,
`result = subprocess.run(` - the only process-spawning call anywhere in the file. (`process_probe.py`
separately holds the string `'wsl.exe'` too, but only as a name to *recognize* an already-running
Windows process as a WSL relay child, never to invoke anything - a broader
`grep -rn "wsl.exe" agent_monitor_for_claude/ --include=*.py` finds that mention alongside several
docstring sentences describing this exact guarantee, none of them a second invocation.)

The boundaries are also enforced by tests, which run without any network access:

```sh
python -m unittest discover -s tests   # backend, including the privacy tests
node --test tests/js/logic.test.js tests/js/global-scope.test.js     # interface logic + the shared-global-scope guard
```

- [tests/test_transcript_privacy.py](tests/test_transcript_privacy.py) plants marker strings in the
  message text, thinking blocks, tool inputs, and tool results of a synthetic transcript, then asserts
  that none of them appear in the data the interface receives. It also asserts that the title comes
  from the first prompt and that a later message's text never appears in that data.
- [tests/test_search.py](tests/test_search.py) asserts that the content search returns session ids
  only, and that a crafted session id cannot point it outside the `projects/` folder.
- [tests/test_tasks.py](tests/test_tasks.py) asserts the task-output boundary: id validation, path
  confinement, redirect confinement, and tail-only reads.
- [tests/test_session_delete.py](tests/test_session_delete.py) asserts the deletion guards: that a
  non-UUID id is rejected, that a session with a live process is refused, that only the named session's
  files are removed, and that the path-confinement helper fails closed when a path cannot be resolved.
- [tests/js/logic.test.js](tests/js/logic.test.js) asserts the display boundary in its "HTML safety"
  group: that escaping covers every character able to leave a text or attribute position, that an
  attribute value cannot break out of its attribute, and that background-task output containing markup
  stays text and yields only class names from the fixed internal list.
- [tests/test_wsl.py](tests/test_wsl.py) guards WSL discovery: `test_no_vmmem_short_circuits` and
  `test_setting_off_short_circuits` assert that `wsl.exe` is never invoked when no WSL2 VM is running or
  when the `wsl` setting is off; `test_discovery_cached_within_ttl` asserts the ten-second cache keeps it
  from running on every poll; `test_stopped_distro_never_globbed` asserts the never-wake guarantee
  directly - a distribution absent from the running list is never looked at, even when its own `.claude`
  directory already exists on disk.
- [tests/test_wsl.py](tests/test_wsl.py)'s `WslProcessStatsTests` guards the process panel's on-demand
  procfs reads: `test_first_call_yields_no_cpu_with_correct_rss_and_uptime` asserts memory and uptime come
  straight from the same scan while a first CPU reading has nothing to diff against yet;
  `test_recycled_child_starttime_resets_cpu_to_none` asserts a recycled pid resets the CPU baseline instead
  of diffing against an unrelated process's ticks; `test_prune_is_scoped_to_this_origin` asserts one
  distribution's cached CPU baselines are never evicted by another distribution's panel refresh.
- [tests/test_search.py](tests/test_search.py)'s `SearchOriginTest` extends the confinement guard to WSL:
  `test_wsl_confinement_refuses_escaping_cwd` asserts a crafted working directory cannot escape a WSL
  root's own `projects/` tree, and `test_unknown_origin_scans_nothing` asserts an origin naming no
  currently running distribution is refused outright rather than falling back to another root's files.
- [tests/test_tasks.py](tests/test_tasks.py)'s `WslRootTasksTest` and `WslRootRedirectTest` extend the
  task-output boundary to WSL: `test_lists_and_reads_a_task_from_the_wsl_root_own_tree` asserts a WSL
  session's own task tree lists and reads exactly like a Windows one;
  `test_follows_absolute_redirect_translated_via_wsl_path_to_windows` and
  `test_redirect_outside_both_roots_is_refused` assert the same scratchpad/project confinement holds once
  a POSIX redirect target is translated to its Windows-readable form.
- [tests/test_session_delete.py](tests/test_session_delete.py)'s `DeleteOriginTest` extends the deletion
  guards to WSL: `test_refuses_unknown_origin`, `test_refuses_live_wsl_session`, and
  `test_treats_a_naming_roots_probe_error_as_live` (a root that cannot be probed is treated as live, never
  as a silent "safe to delete").

If you find any statement in this document that the code does not support, please report it as an
issue - that is a bug in the same sense as any other.

## Contact

For questions about this privacy policy, please open an issue at
https://github.com/jens-duttke/agent-monitor-for-claude/issues
