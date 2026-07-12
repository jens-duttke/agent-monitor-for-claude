# Configuration

All settings are optional. Agent Monitor works out of the box; a settings file only overrides defaults.

## Settings file

Create `agent-monitor-settings.json`. The app never creates or writes this file - you place it manually. It is read from the first of these locations that exists:

1. Next to the executable (or the project root when running from source)
2. `$CLAUDE_CONFIG_DIR/agent-monitor-settings.json` (if `CLAUDE_CONFIG_DIR` is set and differs from `~/.claude/`)
3. `~/.claude/agent-monitor-settings.json`

Invalid JSON or invalid values are reported in a dialog; invalid individual entries are ignored and the default is used.

## Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `poll_interval` | integer (>= 1) | `5` | Seconds between full refreshes of the overview. Independently of this, a cheap change probe runs every second and triggers an immediate refresh when a session or transcript changes. |
| `ended_max_age` | integer (>= 0) | `3600` | How long (seconds) a finished agent stays visible after its process exits. |
| `include_completed` | boolean | `false` | Show finished agents regardless of `ended_max_age`. |
| `subagent_recent_seconds` | integer (>= 1) | `900` | How long (seconds) a subagent transcript is still considered part of the current run - within this window it counts as running (until it ends) or recently finished; older ones are ignored. |
| `window_width` | integer (>= 320) | `920` | Initial window width in logical pixels. |
| `window_height` | integer (>= 240) | `680` | Initial window height in logical pixels. |
| `language` | string | `""` | Force a language by locale code (see below). Empty means auto-detect from the system locale. |

The light/dark appearance is chosen with the toggle in the window's header (it follows your system preference by default), so it is not a settings-file option.

## Example

```json
{
    "poll_interval": 3,
    "include_completed": true,
    "window_width": 1100,
    "language": "de"
}
```

## Languages

`language` accepts any locale code that has a matching file in `locale/`:

`en`, `de`, `es`, `fr`, `hi`, `id`, `it`, `ja`, `ko`, `pt-BR`, `uk`, `zh-CN`, `zh-TW`.

## Token pricing

The per-session cost estimate is computed from `pricing.json` (at the repo root, next to the executable when packaged). This tool never fetches prices - the file is a hand-maintained snapshot of Anthropic's pricing page. Edit it to keep costs accurate.

Top-level keys are **effective-from dates** (`YYYY-MM-DD`). The schedule with the latest date on or before today applies, so a future price change can be entered ahead of time and takes effect automatically on that day. Each date holds a complete table of `model -> rates`, where every rate is in US dollars per million tokens (MTok):

```json
{
    "1970-01-01": {
        "opus-4-8":  { "input": 5, "output": 25, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10 },
        "sonnet-5":  { "input": 2, "output": 10, "cache_read": 0.20, "cache_write_5m": 2.50, "cache_write_1h": 4 }
    },
    "2026-09-01": {
        "sonnet-5":  { "input": 3, "output": 15, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6 }
    }
}
```

- **Model keys** are family-version - the model id with `claude-`, any `[tier]`, and a trailing snapshot date removed (e.g. `claude-opus-4-8[1m]` and `claude-haiku-4-5-20251001` both need `opus-4-8` / `haiku-4-5`). Versions can be priced differently, so list each one you use.
- All five rate fields are explicit (no multipliers), so cache prices that do not follow the usual ratios are handled correctly.
- A session using a model that is not listed shows a plain token total instead of a cost, so nothing is ever priced wrong - add the model to fix it.
- A `_comment` key is allowed for notes and is ignored.
