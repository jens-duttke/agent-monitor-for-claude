# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- An "Error" status for sessions whose turn stopped on an API error and cannot continue - a usage/session limit is named "Usage limit reached", any other API error stays generic - with its own red status color and filter chip. Previously such a session was shown as "Working" indefinitely.
- Click a project's path in its panel header to open that folder in Windows Explorer.

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

[Unreleased]: https://github.com/jens-duttke/agent-monitor-for-claude/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jens-duttke/agent-monitor-for-claude/releases/tag/v0.1.0
