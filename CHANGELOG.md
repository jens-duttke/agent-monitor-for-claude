# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- A session in *Auto-edit* mode no longer reads as "Working" while it waits for a permission prompt. That mode waives the prompt for file edits only - a pending command, fetch or MCP call still asks, and now reads "Needs you". A call with a subagent or workflow running under it keeps reading as "Working".
- A tooltip no longer stays on screen after the element it describes is gone. Rebuilt in place it stays put, genuinely removed it disappears - on the filter chips, which are rebuilt on every refresh, it could previously stay standing indefinitely.
- Sessions running in VS Code now show their permission mode. Only the terminal records a mode switch as its own entry; VS Code notes the mode on each turn, which was not read, so a VS Code session showed no mode at all. This also affects the status: with the mode unknown, a permission prompt could read as "Working" instead of "Needs you".

### Changed
- The row menu's scratchpad entry and the tooltip on a project's path now read *Show scratchpad in Explorer* and *Show in Explorer* - the same wording as the new transcript entry, since all three raise an Explorer window.
- A session stopped by an API error now names the cause instead of reading a bare "Error", so you can tell whether to wait, retry, or sign in again: "Error: servers overloaded", "Error: authentication failed", or "Error: HTTP 521" when only the status code is known. Hovering the status dot adds the error's own message line - Claude Code's wording, in English - which is where details like a usage limit's reset time show up. A usage limit is still called out as before.
- An empty result now says which of the two reasons emptied it. A search that matched nothing reads "The search text was not found in any session" instead of blaming the filter chips, with a line beneath naming what limited the scan - the sessions your chips hide, or the window the *Older* chip is set to.

### Added
- A session's row menu can now show its transcript file in Windows Explorer, selected in its folder - for archiving the raw `.jsonl`, diffing it, or handing it to another tool. The file is only shown, never opened; the entry appears once a transcript exists.
- A search that finds nothing now offers to look wider. Since it only reads the sessions your chips show, one button widens it by one step: first to the sessions the chips hide, then one time window further back. Matches found this way are shown even with their chip off and counted on a line above the list, and keep updating live. Your chips are never changed - the extra rows belong to that one query and go when you edit the search text, or via *Reset* on that same line.
- The released `AgentMonitorForClaude.exe` is now code signed, so Windows can name its publisher instead of reporting an unknown one. Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/). Every release is built by a GitHub Actions workflow from the tagged source in this repository - never on a developer machine - and signed only after a manual approval; see the code signing policy in the README.

## [0.6.1] - 2026-08-16

### Fixed
- A session abandoned in the middle of a tool call no longer claims to be working for the rest of the day - for a VS Code session, days. A pending call with nothing running behind it and a transcript that has stood still for five minutes now reads "Waiting for you". A tool that genuinely takes hours keeps reading "Working" while its process is alive, and a silently thinking agent is untouched.
- A session whose last activity was a local command - a `/compact`, or a `!` shell command - no longer stays on "Working" forever. Recent Claude Code versions record such a command as an ordinary user entry, which looked like an unanswered prompt; both recording formats are now understood. A slash command that briefs the agent still reads "Working", because that one really is awaiting a reply.

## [0.6.0] - 2026-08-15

### Changed
- The *History* chip is now called *Older*. "History" is the browser's word for everything you have ever opened; this chip is the opposite - only the sessions that appear under none of the other chips. Everything else about it is unchanged.
- The *Older* chip now reaches back a chosen distance instead of all the way: its right half opens a window picker (last hour, 24 hours, 7 days, 30 days, or everything) and starts at 24 hours. The scan skips everything outside the window without opening it - with 270 past sessions, the 24-hour listing drops from about two seconds to a fifth of one - and narrowing the window does not re-scan at all. The window also bounds what the content search reads.
- Project panels are now ordered by their most recent activity within each urgency band, instead of alphabetically. A panel whose work finishes slides down to just below the projects still working, instead of disappearing into the middle of a long list. The urgency bands (blocked on you, then working, then idle and finished) are unchanged, and "Priority order" still gives the plain A-Z layout.
- The confirmation before deleting a past session now names it - its title and last-active age, quoted from the row you opened the menu on - and starts the keyboard focus on "Cancel", so pressing Enter or Space to dismiss the dialog can no longer delete anything.
- A session that opens with `/clear` is no longer titled "/clear": the fallback title looks past it to the next real prompt, or to a meaningful opening command like `/pr-review`. Only a session containing nothing but `/clear` still shows it. A command title now also carries its arguments ("/work-on-issue #123"). (thanks to [@jeroenbu](https://github.com/jeroenbu) for the contribution)

### Added
- The Claude Code version a session runs on is now shown, but only when it says something: the column appears as soon as the sessions in view are not all on the same version. A session that outlived a CLI update carries a "+N" badge listing every version it spanned and when each took over. Clicking a version opens that release's changelog in your browser - the app itself still requests nothing (see `PRIVACY.md`).
- The window's page now declares a Content-Security-Policy that forbids it from reaching the network at all, and allows scripts, styles, and images only from the app's own bundled files. A new `PRIVACY.md` states what the app reads, keeps, shows, and writes, with the commands and tests to verify each claim yourself.
- [WSL sessions](https://github.com/jens-duttke/agent-monitor-for-claude/issues/6) - Claude Code sessions running inside a WSL distribution now appear alongside Windows sessions, with status, cost, subagents, history, search, and deletion all working identically and the distro shown as the host. A new `wsl` setting (on by default) turns this off. (thanks to [@jeroenbu](https://github.com/jeroenbu) for the contribution)
- The background-process panel and task-output console now work for WSL sessions too: the per-process CPU, memory, and uptime table is read from the distribution's own process list, and a background task's output - including one redirected to a file in its scratchpad or project folder - streams the same way.

### Fixed
- A session you close after it has been sitting idle no longer vanishes from the overview on the spot. A finished session stays visible for an hour (`ended_max_age`), but that hour was counted from its last activity rather than from when it ended; it now starts when the session ends.
- A project panel now keeps showing its most urgent session status ("Working", "Needs you", ...) in the header while the panel is open - it used to appear only once the panel was collapsed.
- Every session showed up as "Quiet" and no longer reacted to what its agent was doing: recent Claude Code versions record a session's process start time in a different format, which the check for recycled process IDs read as a date in the year 426, so every running session was mistaken for a stale registry entry. Statuses, the busy/idle signal, and the promotion to "Background" are live again, and both formats are accepted.
- Project panels no longer pile up while the window is open: every refresh left an orphaned panel behind instead of reusing the existing one, so the list grew without bound and the window took more and more memory.

## [0.5.0] - 2026-07-18

### Added
- The background-process badge now opens a panel instead of a plain tooltip, with two parts: a live table of the agent's descendant processes (per-process CPU, memory and uptime, refreshed every second), and a list of its background tasks. Each task row shows what it is - the description or command it was started with - and expands to a mono-space console that tails that task's live output, so you can watch progress. The console keeps the task's ANSI colors, and its text can be selected and copied (the refresh pauses while a selection is active). Only tasks from the current run are listed, and a task that redirected its output to a file in its own scratchpad or project folder is followed there. Output is read from disk only while a row is expanded, and only which processes exist plus their resource use ever leave the reader - never any command line.
- For agents working through WSL, the panel adds a clearly-labelled "WSL2 VM" row with the virtual machine's total CPU and memory. WSL runs its Linux processes inside that shared VM, so the Windows-side `wsl.exe` helpers read as idle; the row is marked machine-wide, not this session alone.
- A session's row menu now offers "Open scratchpad", shown only when that session actually has a scratchpad directory.
- The running-subagent badge (⚡) now shows a background workflow's total agent count and leads with it in the tooltip ("Workflow: 12 agents"), instead of only counting the agents running at that instant.

### Fixed
- When you enlarge the window, the area briefly uncovered before WebView2 catches up now shows the app's own background colour instead of a mismatched light or dark edge. It follows the theme you set in the app, not the Windows system theme.
- A session running a background workflow no longer flickers between "Background" and "Idle" - with the ⚡ badge blinking out - during the brief pauses between fan-out phases. The workflow is now tracked as one unit.

## [0.4.0] - 2026-07-16

### Changed
- The header (title, filter chips, and search) now stays fixed at the top while only the session list below it scrolls. The scrollbar sits in that list alone and its space is always reserved, so the layout no longer shifts sideways.

### Fixed
- The space around the session list is now even on all sides: the first project panel sits as far from the top bar as from the window edges, and the excess space after the last panel is gone.
- The abbreviated token count now rounds cleanly at the tier boundaries: "1.0M" instead of "1000k", and "100k" instead of "100.0k".
- A misspelled or unknown key in the settings file is now reported in the settings-error dialog (and ignored), instead of being silently dropped while the default quietly applied. A key starting with an underscore is treated as a comment.
- Sessions that used Claude 3.5 Haiku (often via subagents) now show a dollar cost instead of a plain token total - its price was listed under a key the model id never resolved to.
- A History row whose transcript ends in one very large entry (a giant final tool result) now shows its model and its true last-activity age, instead of a blank model and an age taken from the file's modification time.
- A session that was continued from an earlier one no longer shows the automatic "This session is being continued from..." summary as its title; it now uses the first real prompt, in both the live and History lists.
- Right after a change, the overview no longer briefly shows a stale status or age from an older refresh that finished after a newer one.
- Typing a search query no longer briefly flashes a false "No agents match this filter" before the search actually runs.
- Clicking a filter chip or a search-option toggle right after typing no longer restarts the search - the matches that had already appeared stay put.
- Turning the History chip off and back on while its first load is still running now shows it as active with its loading note, instead of briefly looking inactive.
- When two rows share the same session id (a live window plus a resumed terminal, or a live-and-History duplicate), an open row menu no longer jumps to the other row after the list reorders on a refresh.
- If loading the History list fails, toggling the History chip off and on now retries instead of showing an empty list until the app is restarted.
- History rows' age now keeps counting up while the app is open, instead of staying frozen at the value it had when the list was loaded.
- If saved UI preferences cannot be read at startup (restricted or corrupt browser storage), the app no longer starts with every filter enabled - which would run the History scan unasked and discard your saved selection. History stays off unless you turned it on.
- A failure while jumping to a session's window, opening its project folder, or starting a search no longer briefly replaces the whole overview with an error page.
- Starting two content searches in quick succession no longer occasionally leaves the search stuck on "Searching sessions..." with no results.
- If none of the language files can be loaded (a damaged install), the app now starts in English with default text instead of failing to open at all.
- A content search that fails unexpectedly partway through now shows the search error state instead of presenting the failure as a confident "no session contains this text".
- If "replace the running instance" cannot actually stop the old instance (for example it is running elevated), the app now exits instead of silently starting a second window and taking over the ownership record.
- A session waiting on a question or plan-review dialog now reads "Needs you" even when an unrelated background process is running - such a dialog was previously demoted to "Background".
- The UI now detects Simplified Chinese, Traditional Chinese, Hindi, and Indonesian on Windows systems that report the older descriptive locale names (e.g. "Chinese (Simplified)_China"), instead of falling back to English despite shipping a translation.
- With History shown, resuming a past session no longer lists it twice (a live row plus a stale history row with a broken "Delete"). And a session that ends while the app is running now moves into History on its own instead of disappearing until the next restart.
- A crashed or force-killed session whose leftover registry entry was never cleaned up no longer disappears from both views: once its last activity ages past the retention window it now appears under History.
- A session that is waiting on you but has not yet recorded which prompt it is waiting for no longer shows a misleading label ("Question for you", "Plan review") left over from an already-answered tool. It reads the neutral "Waiting for you" until the prompt is known.
- Sessions that use stdio MCP servers are no longer shown as permanently busy. Such a server runs as a long-lived child process, which was mistaken for a running tool - so an idle session read as "Background" and a session waiting on a permission prompt could read as "Working". Child processes that start together with the session are now recognized as session-lifetime helpers.
- A session's token total and estimated cost could jump too high and stay there: when two refreshes overlapped, a newly appended turn could be counted twice. The incremental usage scan is now serialized.
- Choosing "replace the running instance" no longer risks terminating an unrelated process. If the running instance is closed while the confirmation dialog is open, Windows can reuse its process ID; the app now re-checks at the moment you confirm.
- Subagent workflows are now recognized as finished when they complete. A completed workflow agent's final step is often a tool call rather than a plain closing message, so the ⚡ badge and the "Background" status could stay up until you sent a new prompt.

## [0.3.0] - 2026-07-15

### Added
- A search box in the toolbar that narrows the view to sessions whose transcript *content* contains what you type, with three editor-style toggles - match case, whole word, and regular expression (an invalid pattern turns the box red) - remembered across restarts. It searches only the sessions the active filter chips show, newest first, locally and on demand with a progress bar; matches stream in as they are found, the chip counts follow, and Escape clears it. A running session that newly contains the text appears on its own. Only which sessions matched is ever reported - never any of their content.

## [0.2.0] - 2026-07-13

### Added
- An "Error" status, with its own red colour and filter chip, for sessions whose turn stopped on an API error and cannot continue - a usage/session limit is named "Usage limit reached", any other API error stays generic. Previously such a session was shown as "Working" indefinitely.
- Click a project's path in its panel header to open that folder in Windows Explorer.
- A "History" filter chip (off by default) that lists past sessions that are no longer running - the ones `claude --resume` would show - grouped under their projects. It loads on demand the first time you enable it, so it never slows down the live overview.
- Delete a past session from its row menu (with a confirmation): this permanently removes its transcript and subagent files from disk, and thus from `claude --resume`. It is offered only for finished sessions, refuses any session that still has a live process, and is the only action in the tool that writes anything.

### Changed
- The "Needs you" status is now orange instead of red, so the strongest red is reserved for the new "Error" status.
- The model name and its "+N" switch badge now sit in separate aligned columns - the name left-aligned, the badge right-aligned - so both line up across every session row.
- "New" is now a regular filter chip alongside the others - shown by default and unchecked to hide - instead of a separate visibility toggle that was off by default.
- The filter chips are now ordered attention-first: the states that want you (Needs you, Error, Interrupted, New, Idle) come before the ones that do not (Working, Background, Quiet).
- Each filter chip now has a tooltip on hover, briefly explaining what that status means and when it occurs, translated into every language.
- The filter chips and toolbar controls now use a flatter, moderately-rounded shape modeled on Claude.ai instead of fully-rounded pills. Plain buttons and the sort dropdown carry a visible fill and show their border only on hover or keyboard focus; the toggle controls keep a resting border.
- Active and inactive filter chips - and the priority-order toggle - are now clearly distinct: inactive is faded and hollow, active is solid and fully lit, where before they differed only by text colour and border.
- The sort dropdown now reads like a Claude.ai select field - the value on the left, a thin chevron pinned to the right edge, full-strength text - instead of a compact dimmed chip.
- The sort-direction button now has a tooltip on hover, translated into every language.
- A filter chip no longer shows a "0" count - the number appears only when at least one session matches.

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

[Unreleased]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jens-duttke/agent-monitor-for-claude/releases/tag/v0.1.0
