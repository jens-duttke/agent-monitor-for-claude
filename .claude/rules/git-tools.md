---
paths:
  - "agent_monitor_for_claude/git_*.py"
---
- Git never runs on the render or poll path; the overview answers from cache at once and refreshes on daemon threads. Nothing contacts a remote - the remote side is the remote-tracking refs.
- A diff path is validated before git runs (`_valid_relative_path`) and against the top level after; the output reaches the page only through `logic.diffToHtml` (`esc` per line).
