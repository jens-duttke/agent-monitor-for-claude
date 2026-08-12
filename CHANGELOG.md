# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The Claude Code version a session runs on is now shown - but only when it says something: the column appears as soon as the sessions in view are not all on the same version, so an agent still running last week's CLI stands out, while an all-current list stays exactly as it was. A session that outlived a CLI update carries a "+N" badge listing every version it spanned and the time each took over, which dates a mid-session change in the agent's behaviour. Clicking a version number opens that release's changelog entry in your browser - the app itself still requests nothing (see `PRIVACY.md`).

### Fixed
- Every session showed up as "Quiet" and no longer reacted to what its agent was doing: recent Claude Code versions record a session's process start time in a different format, which the check for recycled process IDs read as a date in the year 426 - so every running session was mistaken for a stale registry entry and reported as ended. Statuses, the "busy"/"idle" signal, and the promotion to "Background" while subagents or workflows run are all live again, and sessions no longer disappear from the list once their last activity is a few minutes old. Both formats are accepted, so older Claude Code versions keep working.

## [0.6.0] - 2026-08-06

### Changed
- The confirmation before deleting a past session now names the session it is about to delete - its title and how long ago it was last active, quoted from the row you opened the menu on. The keyboard focus also starts on "Cancel" instead of "Delete", so pressing Enter or Space to dismiss the dialog can no longer delete anything. Deletion is still permanent, but confirming it is no longer a generic prompt that looks the same for every session.
- A session that opens with `/clear` is no longer titled "/clear": the fallback title now looks past that housekeeping entry to the first thing that can actually name the session - the next real prompt, or a meaningful opening command like `/pr-review` (which keeps the title, exactly as before). Only a session containing nothing but `/clear` still shows it. A command title now also carries its arguments ("/work-on-issue #123" instead of just "/work-on-issue"), clipped like any other title. (thanks to [@jeroenbu](https://github.com/jeroenbu) for the contribution)

### Added
- The released `AgentMonitorForClaude.exe` is now code signed, so Windows can name its publisher instead of reporting an unknown one. Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/). Every release is now built by a GitHub Actions workflow from the tagged source in this repository - never on a developer machine - and signed only after a manual approval; see the code signing policy in the README.
- The window's page now declares a Content-Security-Policy that forbids it from reaching the network at all: no requests, and no scripts, styles, or images from anywhere but the app's own bundled files. The app never made a network request in the first place - now the browser engine would refuse one even if asked. A new `PRIVACY.md` states what the app reads, keeps, shows, and writes, with the commands and tests to verify each claim yourself.
- [WSL sessions](https://github.com/jens-duttke/agent-monitor-for-claude/issues/6) - Claude Code sessions running inside a WSL distribution now appear alongside Windows sessions - status, cost, subagents, history, search, and deletion all work identically, and the distro is shown as the session's host. A new `wsl` setting (on by default) turns this off. (thanks to [@jeroenbu](https://github.com/jeroenbu) for the contribution)
- The background-process panel and task-output console now work for WSL sessions too: the live per-process CPU, memory, and uptime table is read straight from the distribution's own process list, and a background task's output - including one redirected to a file in its scratchpad or project folder - streams the same way it does for a Windows session.

### Fixed
- Project panels no longer pile up while the window is open: every refresh left an orphaned panel behind instead of reusing the existing one, so the list grew without bound and the window slowly took more and more memory.

## [0.5.0] - 2026-07-18

### Added
- The background-process badge now opens a panel (click it) instead of a plain tooltip. It has two parts: a live table of the agent's descendant processes with per-process CPU, memory, and how long each has been running (refreshed every second), and a list of the agent's background tasks. Only tasks from the current run are listed - one whose output was last written before the running processes even started is a leftover from an earlier run and is hidden. Each task row shows what it is (the description or command it was started with) and expands to a mono-space console that tails that task's live output, so you can watch progress and estimate how long it still needs. The console colorizes the task's ANSI terminal colors (dropping the cursor-control codes a plain view cannot honor), and its text can be selected and copied with Ctrl+C - the live refresh pauses while a selection is active so it is not lost. When a task redirected its output to a file in its own scratchpad or project folder (so its default log stays empty), the panel follows that redirect and shows the real output. The task output is read from disk only while a row is expanded, and only which processes exist plus their resource use ever leave the reader - never any command line.
- For agents working through WSL, the panel adds a clearly-labelled "WSL2 VM" row with the virtual machine's total CPU and memory. WSL runs its Linux processes inside that shared VM, so the Windows-side `wsl.exe` helpers read as idle; the VM row is where the real usage shows. It is marked as machine-wide (shared across all WSL distributions and sessions), not this session alone.
- A session's row menu now offers "Open scratchpad" (opens the session's scratchpad folder in Explorer), shown only when that session actually has a scratchpad directory.
- The running-subagent badge (⚡) now shows a background workflow's total agent count, and its tooltip leads with it (e.g. "Workflow: 12 agents"). Previously it only counted the agents running at that instant, so a workflow whose earlier agents had already finished looked smaller than it was.

### Fixed
- When you enlarge the window, the area briefly uncovered before WebView2 catches up now shows the app's own background colour instead of a mismatched light or dark edge. The window background follows the theme you actually set in the app, not the Windows system theme, so it stays correct even when the two differ.
- A session running a background workflow no longer flickers between "Background" and "Idle" - with the ⚡ badge blinking out - during the brief pauses between a workflow's fan-out phases, when no single agent is momentarily running. The workflow is now tracked as one unit, so the session reads as busy for as long as the workflow is actually running.

## [0.4.0] - 2026-07-16

### Changed
- The header (title, filter chips, and search) now stays fixed at the top while only the session list below it scrolls. The scrollbar sits in that list alone and its space is always reserved, so the layout no longer shifts sideways when the list grows tall enough to need it.

### Fixed
- The space around the session list is now even on all sides: the first project panel sits as far from the top bar as from the window edges, and the excess space after the last panel is gone.
- The abbreviated token count now rounds cleanly at the tier boundaries: a total just under a million reads "1.0M" instead of "1000k", and one just under 100k reads "100k" instead of "100.0k".
- A misspelled or unknown key in the settings file is now reported in the settings-error dialog (and ignored), instead of being silently dropped while the default quietly applied. A key starting with an underscore is treated as a comment and left alone.
- Sessions that used Claude 3.5 Haiku (often via subagents) now show a dollar cost instead of a plain token total. Its price was listed under a key the model id never resolved to, so the cost estimate was silently skipped for the whole session.
- A History row whose transcript ends in one very large entry (a giant final tool result) now shows its model and its true last-activity age, instead of a blank model and an age taken from the file's modification time.
- A session that was continued from an earlier one no longer shows the automatic "This session is being continued from..." summary as its title; it now uses the first real prompt, in both the live and History lists.
- Right after a change, the overview no longer briefly shows a stale status or age from an older refresh that finished after a newer one; only the most recent refresh is applied.
- Typing a search query no longer briefly flashes a false "No agents match this filter" in the moment between the keystroke and the search actually running.
- Clicking a filter chip or a search-option toggle right after typing no longer restarts the search a moment later - the matches that had already appeared stay put instead of vanishing and reloading.
- Turning the History chip off and back on again while its first load is still running now immediately shows the chip as active with its loading note, instead of briefly looking inactive until the load finishes.
- When two rows share the same session id (a live window plus a resumed terminal, or a live-and-History duplicate), an open row menu no longer jumps to the other row after the list reorders on a refresh.
- If loading the History list fails, toggling the History chip off and on now retries instead of showing an empty list permanently until the app is restarted.
- History rows' age now keeps counting up while the app is open, instead of staying frozen at the value it had when the History list was loaded.
- If saved UI preferences cannot be read at startup (restricted or corrupt browser storage), the app no longer starts with every filter enabled - which would run the History scan without being asked and discard the saved filter selection. Your saved filters are preserved, and History stays off unless you turned it on.
- A failure while jumping to a session's window, opening its project folder, or starting a search no longer briefly replaces the whole overview with an error page; the failed action is now contained.
- Starting two content searches in quick succession (for example toggling a search option mid-typing) no longer occasionally leaves the search stuck on "Searching sessions..." with no results.
- If none of the language files can be loaded (a damaged install), the app now starts in English with default text instead of failing to open at all.
- If a content search fails unexpectedly partway through, it now shows the search error state instead of presenting the failure as a confident "no session contains this text".
- If "replace the running instance" cannot actually stop the old instance (for example it is running elevated), the app now detects that it did not become the sole instance and exits instead of silently starting a second window and taking over the ownership record.
- A session waiting on a question or plan-review dialog now reads "Needs you" even when an unrelated background process is running. Such a dialog was previously demoted to "Background" whenever the session had any live child process, hiding that it was actually waiting on you.
- The UI now detects Simplified Chinese, Traditional Chinese, Hindi, and Indonesian on Windows systems that report the older descriptive locale names (e.g. "Chinese (Simplified)_China"). These languages previously fell back to English despite shipping a translation, unless the language was set manually.
- With History shown, resuming a past session no longer lists it twice (a live row plus a stale history row with a broken "Delete"); the stale history row is dropped once the session is live again. And a session that ends while the app is running now moves into History on its own instead of disappearing until the next restart.
- A crashed or force-killed session whose leftover registry entry was never cleaned up no longer disappears entirely. Once its last activity aged past the retention window it was dropped from the live overview yet still skipped by History; it now appears under History instead of vanishing from both views.
- A session that is waiting on you but has not yet recorded which prompt it is waiting for no longer shows a misleading label (e.g. "Question for you" or "Plan review") left over from an earlier, already-answered tool. It now reads the neutral "Waiting for you" until the specific prompt is known.
- Sessions that use stdio MCP servers are no longer shown as permanently busy. Such a server runs as a long-lived child process of the session, which was mistaken for a running tool - so an idle session read as "Background" and a session waiting on a permission prompt could read as "Working", hiding that it needed you. Child processes that start together with the session are now recognized as session-lifetime helpers and no longer counted as a running tool.
- A session's token total and estimated cost could sometimes jump too high and stay there. When two overview refreshes overlapped, a newly appended turn could be counted twice; the incremental usage scan is now serialized so each turn is summed exactly once.
- Choosing "replace the running instance" no longer risks terminating an unrelated process. If the running instance is closed while that confirmation dialog is open, its process ID can be reused by Windows for another program; the app now re-checks that the instance is still running at the moment you confirm, and does nothing if it has already exited.
- Subagent workflows are now recognized as finished when they complete. A completed workflow agent's final step is often a tool call rather than a plain closing message, which the previous check did not treat as done - so the running-subagent badge (⚡) and the "Background" status could stay up after a workflow had actually finished, clearing only once you sent a new prompt. Completion is now detected from the transcript's final turn regardless of how it ended.

## [0.3.0] - 2026-07-15

### Added
- A search box in the toolbar that narrows the view to sessions whose transcript *content* contains what you type. Three editor-style toggles refine it - match case, match whole word, and use a regular expression (an invalid pattern turns the box red), remembered across restarts. It searches only the sessions the active filter chips currently show, newest first, running locally and on demand with a progress bar, and matches stream in as they are found; the chip counts update to match, and Escape clears it. Results stay current as your agents work - a running session that newly contains the text appears on its own. The search only ever reports which sessions matched - never any of their content - and, like everything else, nothing leaves your machine.

## [0.2.0] - 2026-07-13

### Added
- An "Error" status for sessions whose turn stopped on an API error and cannot continue - a usage/session limit is named "Usage limit reached", any other API error stays generic - with its own red status color and filter chip. Previously such a session was shown as "Working" indefinitely.
- Click a project's path in its panel header to open that folder in Windows Explorer.
- A "History" filter chip (off by default) that lists past sessions that are no longer running - the ones `claude --resume` would show - grouped under their projects. It loads on demand the first time you enable it, so it never slows down the live overview.
- Delete a past session from its row menu (with a confirmation): this permanently removes its transcript and subagent files from disk, and thus from `claude --resume`. It is offered only for finished sessions, refuses any session that still has a live process, and is the only action in the tool that writes anything - everything else stays read-only.

### Changed
- The "Needs you" status is now orange instead of red, so the strongest red is reserved for the new "Error" status - a session that cannot continue at all now outranks one merely waiting on you.
- The model name and its "+N" switch badge now sit in separate aligned columns - the name left-aligned, the badge right-aligned - so both line up cleanly across every session row.
- "New" is now a regular filter chip alongside the others - shown by default and unchecked to hide - instead of a separate visibility toggle that was off by default.
- The filter chips are now ordered attention-first: the states that want you (Needs you, Error, Interrupted, New, Idle) come before the ones that do not (Working, Background, Quiet).
- Each filter chip now has a tooltip on hover, briefly explaining what that status means and when it occurs, translated into every language.
- The filter chips and toolbar controls now use a flatter, moderately-rounded shape modeled on Claude.ai, instead of the previous fully-rounded pill shape. Plain buttons and the sort dropdown carry a visible fill and show their border only on hover or keyboard focus; the toggle controls (filter chips, priority order) keep a resting border.
- Active and inactive filter chips - and the priority-order toggle - are now clearly distinct: an inactive one is a faded, hollow chip and an active one is solid and fully lit, where before they differed only by text color and border.
- The sort dropdown now reads like a Claude.ai select field - the value on the left, a thin chevron pinned to the right edge, full-strength text - instead of a compact dimmed chip.
- The sort-direction button now has a tooltip on hover, translated into every language.
- A filter chip no longer shows a "0" count - the number appears only when at least one session matches, so the chips with sessions stand out at a glance.

### Fixed
- The expanded usage breakdown kept a gap before the cost again, instead of running its last entry straight into it.

## [0.1.0] - 2026-07-12

### Added
- Initial release: a local, fully offline window showing every running Claude Code session, grouped by project, with each session's live status refreshed every few seconds and updated in place - no flicker, no scroll jump, and open menus stay open.
- Each session's status is a colored dot (label on hover), forming a traffic-light gradient: "Needs you" (red), "Working", "Background", "Idle" (green), "Interrupted" (yellow), and "Quiet".
- Sessions blocked on you are labeled by what they wait for: a question dialog, a plan review, or a permission prompt.
- A "Background" status marks a session busy with subagents or its own background process, so it is not mistaken for finished.
- An "Interrupted" status distinguishes a session you stopped mid-task from one that finished on its own.
- Each session shows its current permission mode (Manual, Auto, Auto-edit, Plan).
- A banner lists the sessions that need your feedback to continue, with a one-click jump to each.
- Projects are ordered by urgency, with a "Priority order" toggle for a plain A-Z layout that also orders the sessions within each project by status.
- Sessions can be sorted by activity, usage, model capability, host, or status - ascending or descending.
- One filter chip per status (each chip's dot doubles as the color key), with your selection remembered across restarts; "New" windows have their own visibility toggle.
- Sessions are shown with the same title Claude Code displays, paired with the session's estimated cost.
- A per-session menu (⋯ button) with "Copy session ID".
- Each session shows the model it currently uses; a "+N" badge reveals the model-switch timeline on hover.
- Each session shows a single estimated cost in whole dollars ("$19", or "<$1" below a dollar), expandable on hover to the full per-tier breakdown. Cost is computed per model and driven by an editable `pricing.json` you maintain by hand.
- A badge shows how many subagents a session is running (and recently finished), with a tooltip listing what each is doing.
- A badge shows the background OS processes a session is running (e.g. a watched build or scan), with the process names in the tooltip.
- The header shows the globally configured default effort level.
- Collapsible project panels; a collapsed panel summarizes its most urgent session status.
- Host application shown per session (VS Code, JetBrains IDEs, terminals, and others), with a CLI marker for terminal-driven sessions.
- Clicking a session brings its hosting window to the foreground; VS Code extension sessions jump tab-exact via the official deep link (requires extension v2.1.72 or newer).
- Live-ticking activity age per session.
- Light and dark theme with a toggle in the header, following the system preference by default.
- Fully local, read-only operation - no network, no credentials, and nothing ever leaves your machine.
- 13 languages, auto-detected from the system locale.
- Optional settings file to tune the poll interval and window size.

[Unreleased]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jens-duttke/agent-monitor-for-claude/releases/tag/v0.1.0
