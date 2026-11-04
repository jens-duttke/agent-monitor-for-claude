'use strict';

/* Agent Monitor for Claude - UI controller, shared core.

   The Python bridge is a pure data provider: get_snapshot returns raw session
   records, and all derivation (status, formatting, grouping, sorting) happens
   in logic.js. The controller consumes that logic, renders the overview with
   granular DOM reconciliation (no full rebuilds - open menus and scroll
   position survive a refresh), and owns the DOM/bridge side effects. Opened
   directly in a browser (a file:// page, or ?mock) there is no bridge, so it
   loads the unbundled dev-mock.js and renders from that mock data instead.

   The controller is split by concern into classic scripts that share one
   global scope, loaded in the order boot-scripts.js lists them. This file
   comes first: the error bridge, the shared state, the labels and filter
   definitions, and the bridge access every other file builds on. index.js
   comes last and starts everything; the files in between only declare, so
   none of them may run code at load time that needs a later one. */

const logic = window.AMC_LOGIC;

// A UI error would otherwise be swallowed inside WebView2 (no terminal, no
// console). Surface it: forward to the Python bridge (-> stderr) and show it
// in the window instead of leaving the loading skeleton up forever.
function reportUiError(detail) {
    const message = detail && detail.stack ? detail.stack : String(detail);
    try {
        const bridge = (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
        if (bridge && typeof bridge.log === 'function') {
            // Contain an async rejection inline (not via logic.settleCall, which
            // may not have loaded): a rejected bridge.log would otherwise re-enter
            // unhandledrejection -> reportUiError and loop.
            const pending = bridge.log(message);
            if (pending && typeof pending.then === 'function') {
                pending.then(undefined, () => {});
            }
        }
    } catch (e) { /* bridge unavailable */ }
    try {
        const content = document.getElementById('content');
        if (content) {
            // Escaped inline, not via logic.esc: this handler has to survive
            // logic.js itself failing to load, which is one of the errors it
            // reports. `&` and `<` are the two that matter in text position -
            // and `&` first, so the entities below are not re-escaped.
            content.innerHTML = '<div class="empty error">UI error - see details below:\n\n'
                + message.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</div>';
        }
    } catch (e) { /* DOM unavailable */ }
}

window.addEventListener('error', (event) => reportUiError(event.error || event.message));
window.addEventListener('unhandledrejection', (event) => reportUiError(event.reason));

const state = {
    labels: {},
    labelsLoaded: false,
    pricing: {},
    // What each ticket's sessions cost (logic.ticketTotals), rebuilt on every
    // render, and whether the past sessions were loaded into it.
    ticketTotals: new Map(),
    ticketIncludesHistory: false,
    // The global default effort the header names; a row names its own effort
    // only when it differs.
    defaultEffort: '',
    pollInterval: 5,
    filters: new Set(),
    // Free-text search over the session CONTENT (transcript text), in-session
    // only (deliberately not persisted, so a restart never hides everything
    // behind a stale query). The match is computed by the Python bridge, which
    // reads the transcripts and returns each hit session's id, its hit count and
    // a few short excerpts - bounded there, and all this side ever holds.
    // `searchMatches` is that Map, keyed by session id; null means no content
    // filter is active yet.
    search: '',
    searchMatches: null,
    searchLoading: false,
    searchProcessed: 0,
    searchTotal: 0,
    searchSeq: 0,
    searchTimer: null,
    // Editor-style search options (persisted), plus whether the current query is
    // an invalid regular expression (turns the box red).
    searchMatchCase: false,
    searchWholeWord: false,
    searchRegex: false,
    searchError: false,
    // Set only by the empty result's widening offer: the search then reads past
    // the active chips (see logic.searchScopeRefs) and its extra hits are shown
    // even though their chip is off. It belongs to the QUERY, not to the chips -
    // toggling a chip keeps it, and any edit to the search text drops it, so the
    // widened view is a one-off answer that disappears with the question.
    searchIncludeHidden: false,
    sort: 'activity',
    sortDir: 'asc',
    priorityOrder: true,
    collapsed: new Set(),
    last: null,
    receivedAt: null,
    // Epoch (ms) when the history list was fetched. History age_seconds is frozen
    // at that moment, so history rows must age from here, not from receivedAt (the
    // latest live-snapshot poll), or their age would freeze.
    historyReceivedAt: null,
    fingerprint: null,
    checking: false,
    // Monotonic token so an older, slower snapshot cannot render over a newer
    // one when the interval poll and a fingerprint-triggered tick overlap.
    snapshotGen: 0,
    booted: false,
    // Past (non-live) sessions, fetched on demand the first time the history
    // chip is enabled and cached thereafter (dead sessions do not change). null
    // means "not fetched yet"; historyLoading guards against a double fetch and
    // drives the loading note.
    history: null,
    historyLoading: false,
    // How far back the history chip reaches (a logic.HISTORY_RANGES key), and
    // the window the cache above was actually fetched for. The two differ while
    // a narrower selection is served from a wider fetch; undefined means the
    // cache holds nothing yet.
    historyRange: logic.DEFAULT_HISTORY_RANGE,
    historyLoadedSeconds: undefined,
    // Sessions per filter bucket as of the last render, kept so the filter list
    // shows its counts the moment it is opened rather than at the next poll.
    filterCounts: {},
    // Window preferences from the header toggles (see WINDOW_TOGGLES).
    compact: false,
    onTop: false,
};

// Sort options: key -> label key. Values are computed per session in sort.js.
const SORT_DEFS = [
    ['activity', 'sort_activity'],
    ['usage', 'sort_usage'],
    ['model', 'sort_model'],
    ['host', 'sort_host'],
    ['status', 'sort_status'],
];

// Interval (ms) for the cheap change-fingerprint probe between full polls.
const FINGERPRINT_INTERVAL = 1000;

// English fallbacks so the UI never renders empty text, even if the
// bridge delivers its translations late.
const DEFAULT_LABELS = {
    app_title: 'Agent Monitor for Claude',
    subtitle: 'Live status of your Claude Code agents',
    status_working: 'Working',
    status_processing: 'Background',
    status_interrupted: 'Interrupted',
    status_errored: 'Error',
    status_errored_reason: 'Error: {reason}',
    status_usage_limit: 'Usage limit reached',
    error_overloaded: 'Servers overloaded',
    error_server: 'Server-side problem',
    error_invalid_request: 'Request rejected',
    error_auth: 'Authentication failed',
    error_http: 'HTTP {status}',
    status_awaiting_input: 'Idle',
    status_awaiting_permission: 'Permission needed',
    status_needs_you: 'Waiting for you',
    status_question: 'Question for you',
    status_plan_review: 'Plan review',
    status_new: 'New',
    status_completed: 'Finished',
    status_unknown: 'Unknown',
    status_sleeping: 'Wakes in {time}',
    status_retrying: 'Retrying ({attempt}/{max})',
    status_offline: 'Offline - retrying ({attempt}/{max})',
    status_blocked: 'Blocked - needs your decision',
    status_asks: 'Finished with a question',
    status_for: 'for {time}',
    status_limit_resets: '{limit} - resets in {time}',
    status_limit_over: '{limit} reset - you can continue',
    limit_five_hour: '5-hour limit',
    limit_seven_day: 'Weekly limit',
    limit_usage: 'Usage limit',
    origin_task: 'Started by a finished background task',
    origin_peer: 'Started by a message from another session',
    feedback_needed: '{count} waiting for your answer',
    sort_activity: 'Last activity',
    sort_usage: 'Usage',
    sort_model: 'Model',
    sort_host: 'Host',
    host_wsl_tip: 'This agent runs inside the WSL distribution “{distro}”. Its files are read over \\\\wsl.localhost; nothing is ever executed inside the distribution.',
    cli_changelog_tip: 'Claude Code {version} - opens the changelog for this version in your browser',
    sort_status: 'Status',
    priority_order: 'Priority order',
    priority_order_hint: 'Sort projects by attention - the ones that need you first',
    sort_dir_newest: 'Newest first',
    sort_dir_oldest: 'Oldest first',
    sort_dir_lowest: 'Lowest first',
    sort_dir_highest: 'Highest first',
    sort_dir_smallest: 'Smallest model first',
    sort_dir_largest: 'Largest model first',
    sort_dir_a_z: 'A to Z',
    sort_dir_z_a: 'Z to A',
    sort_dir_urgent: 'Most urgent first',
    sort_dir_calm: 'Least urgent first',
    empty_state: 'No active Claude Code agents.',
    empty_filter: 'No agents match this filter.',
    search_placeholder: 'Search sessions',
    search_loading: 'Searching sessions…',
    search_match_case: 'Match case',
    search_whole_word: 'Match whole word',
    search_regex: 'Use regular expression',
    search_regex_invalid: 'Invalid regular expression',
    search_no_match: 'The search text was not found in any session.',
    search_scope_hint: 'Only the sessions currently shown were searched.',
    search_widen_hidden: 'Also search hidden sessions',
    search_widen_range: 'Search further back ({range})',
    search_widened_note: 'Outside the active filters: {count}',
    search_widened_reset: 'Reset',
    search_widened_reset_tip: 'Drop the extra hits and search only what your filters show. Your search text stays.',
    last_activity: 'Last activity {age} ago',
    no_activity: 'No activity yet',
    tool_running: 'tool running',
    kind_interactive: 'Interactive',
    kind_background: 'Background',
    copy_attach_command: 'Copy attach command',
    filter_needs: 'Needs you',
    filter_errored: 'Error',
    filter_interrupted: 'Interrupted',
    filter_idle: 'Idle',
    filter_working: 'Working',
    filter_background: 'Background',
    filter_quiet: 'Quiet',
    filter_all: 'All current',
    filter_none: 'Nothing selected',
    filter_needs_tip: 'The agent stopped and needs you - to grant a permission, answer a question, or approve a plan.',
    filter_errored_tip: "An API error or usage limit stopped the last run - it can't continue until that's resolved.",
    filter_interrupted_tip: "You stopped the agent mid-task, so it's your turn again - nothing is running.",
    filter_new_tip: "A just-started session that hasn't written anything to its transcript yet.",
    filter_idle_tip: 'The agent finished and is waiting for your next message. Nothing is required.',
    filter_working_tip: 'The agent is busy - thinking, running a tool, or working on your prompt.',
    filter_background_tip: 'The agent finished, but a subagent or background process is still running, or it will wake itself up later.',
    filter_quiet_tip: 'Finished or inactive sessions - nothing needs you and nothing is running.',
    age_seconds: '{s}s',
    age_minutes: '{m}m',
    age_hours: '{h}h {m}m',
    age_days: '{d}d',
    token_summary: '{input} in · {output} out',
    token_cache_read: '{cache_read} cache read',
    token_cache_5m: '{cache_5m} 5m write',
    token_cache_1h: '{cache_1h} 1h write',
    token_cache_write: '{cache_write} cache write',
    effort_badge: 'Default effort: {level}',
    auto_paused: 'Auto mode paused - Claude Code is asking you again',
    auto_denials: 'Calls blocked by the classifier: {count}',
    worktree_tip: 'Works in a git worktree',
    worktree_branch: 'Branch: {branch}',
    branch_tip: 'Git branch: {branch}',
    pr_tip: 'Pull requests linked to this session',
    context_tip: 'Context: {used} of {size} tokens ({percent}%)',
    context_used: 'Context: {used} tokens',
    cache_warm_tip: 'Prompt cache warm for another {time}',
    cache_cold_tip: 'Prompt cache expired - the next prompt writes {tokens} tokens again ({cost})',
    cache_cold_tokens_tip: 'Prompt cache expired - the next prompt writes {tokens} tokens again',
    compactions: 'Context compactions: {count}',
    files_edited: 'Files edited: {count}',
    artifacts: 'Artifacts: {count}',
    cost_main: 'Main conversation: {cost}',
    cost_agents: 'Subagents: {cost}',
    cost_agent_unknown: 'other',
    cost_skills: 'By skill:',
    cost_web_search: 'Web searches: {count} · {cost}',
    ticket_cost: 'Ticket {ticket}: {cost} · sessions: {count}',
    ticket_scope_live: 'Running and recently ended sessions only - turn on “Older” to add past ones',
    ticket_scope_history: 'Includes the older sessions of the selected time range',
    plan_tip: 'Plan: step {step} of {total}',
    queued_tip: 'Queued messages: {count}',
    budget_tip: 'Budget: {used} of {total} used',
    cli_update_tip: 'Claude Code {available} is available - this agent still runs {running}. Restart it to switch.',
    hook_errors_tip: 'Hooks failing without blocking - check your hook configuration',
    hook_error_line: '{hook} (exit code {code}): {count}',
    effort_tip: 'Effort of the latest reply: {level}',
    fast_mode_tip: 'Fast mode is on',
    remote_control_tip: 'Remote Control is on - continue this agent from claude.ai or the Claude app',
    subagents_running: 'Running subagents: {count}',
    subagents_finished: 'Recently finished: {count}',
    subagents_workflow: 'Workflow: {done}/{total} agents',
    processes_running: 'Background processes: {count}',
    proc_open_hint: 'Show CPU, memory and task output',
    proc_close: 'Close',
    proc_processes_title: 'Processes',
    proc_tasks_title: 'Background tasks',
    proc_col_process: 'Process',
    proc_col_cpu: 'CPU',
    proc_col_memory: 'Memory',
    proc_col_uptime: 'Uptime',
    proc_none: 'No background processes.',
    proc_wsl_vm: 'WSL2 VM (all distributions)',
    proc_wsl_vm_tip: 'The whole WSL2 virtual machine, shared across all distributions and sessions - not this session alone. WSL runs Linux processes inside this VM, so they cannot be listed individually.',
    proc_tasks_none: 'No background-task output - only run_in_background tasks write a live log.',
    task_label: 'Task {id}',
    task_active: 'active',
    task_last_output: 'last output {age} ago',
    task_output_loading: 'Loading output…',
    task_output_empty: '(no output yet)',
    tasks_more: '+{count} older',
    row_menu: 'More actions',
    copy_session_id: 'Copy session ID',
    copied: 'Copied to clipboard',
    show_in_file_manager: 'Show in file manager',
    project_this_computer: 'This computer',
    show_transcript: 'Show transcript in file manager',
    show_scratchpad: 'Show scratchpad in file manager',
    filter_history: 'Older',
    filter_history_tip: 'Older sessions that are no longer running and have left the overview (loaded on demand)',
    history_loading: 'Loading older sessions…',
    history_range_tip: 'How far back older sessions are listed. A shorter window also makes the scan faster.',
    history_range_hint: 'Only older sessions inside the selected window are listed and searched. Widen it to look further back.',
    history_range_1h: '1 h',
    history_range_24h: '24 h',
    history_range_7d: '7 days',
    history_range_30d: '30 days',
    history_range_all: 'All',
    delete_session: 'Delete session',
    delete_confirm_title: 'Delete this session?',
    delete_confirm_body: 'This permanently removes the session transcript and its subagent files from disk. This cannot be undone.',
    delete_confirm_ok: 'Delete',
    cancel: 'Cancel',
    deleted: 'Session deleted',
    delete_failed: 'Could not delete the session',
    compact_view: 'Compact view - only status, title and age per agent',
    keep_on_top: 'Keep the window above other windows',
};

// One filter per status color, attention-first: the states that want you
// (blocked, errored, interrupted, new, idle) come before the ones that don't
// (busy, background, quiet). Each is a checkbox for its status band: all on by
// default, and unchecking one hides those sessions. Each dot teaches its color,
// so the filter list also serves as the status legend.
const FILTER_DEFS = [
    { key: 'needs', label: 'filter_needs', tip: 'filter_needs_tip', dot: 'dot-needs' },
    { key: 'errored', label: 'filter_errored', tip: 'filter_errored_tip', dot: 'dot-errored' },
    { key: 'interrupted', label: 'filter_interrupted', tip: 'filter_interrupted_tip', dot: 'dot-interrupted' },
    { key: 'new', label: 'status_new', tip: 'filter_new_tip', dot: 'dot-new' },
    { key: 'idle', label: 'filter_idle', tip: 'filter_idle_tip', dot: 'dot-idle' },
    { key: 'working', label: 'filter_working', tip: 'filter_working_tip', dot: 'dot-working' },
    { key: 'background', label: 'filter_background', tip: 'filter_background_tip', dot: 'dot-background' },
    { key: 'quiet', label: 'filter_quiet', tip: 'filter_quiet_tip', dot: 'dot-quiet' },
    // Off by default: enabling it triggers the on-demand scan of past sessions.
    // Labelled "Older" in the UI - the key stays `history` because it is part of
    // the persisted filter selection (and of is_history / get_history).
    { key: 'history', label: 'filter_history', tip: 'filter_history_tip', dot: 'dot-history', offByDefault: true },
];

// The status buckets the chips can select - the valid, persistable filter keys.
const FILTER_KEYS = new Set(FILTER_DEFS.map((def) => def.key));

// The chips active on a first launch: every chip except the ones that opt out
// (history), so the potentially large history scan only runs once the user asks
// for it. Derived in logic.js so the "never a fallback to all chips" rule is
// covered by a test.
const DEFAULT_FILTER_KEYS = logic.defaultFilterKeys(FILTER_DEFS);

function apiBridge() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
}

function mockMode() {
    return !!window.__MOCK_BOOTSTRAP__;
}

// A browser preview (no pywebview bridge) renders from mock data. That data
// lives in dev-mock.js, a sibling deliberately never bundled (see the spec), so
// the packaged app - always served over http with the bridge present - has no
// code path that reads fabricated session data. The preview is requested only
// from a file:// page or an explicit ?mock query, never in the packaged app.
function devPreviewRequested() {
    try {
        return location.protocol === 'file:' || new URLSearchParams(location.search).has('mock');
    } catch (e) {
        return false;
    }
}

let devMockLoading = false;

function loadDevMock(done) {
    if (devMockLoading) {
        return;
    }
    devMockLoading = true;
    const script = document.createElement('script');
    script.src = 'dev-mock.js';
    // A missing file (e.g. the packaged app, where it is never shipped) simply
    // leaves the loading skeleton up - never an error the user has to see.
    script.onload = done;
    script.onerror = done;
    document.head.appendChild(script);
}

async function callBootstrap() {
    const bridge = apiBridge();
    if (bridge) {
        return bridge.get_bootstrap();
    }
    return window.__MOCK_BOOTSTRAP__ || { labels: {}, poll_interval: 5 };
}

async function callSnapshot() {
    const bridge = apiBridge();
    if (bridge) {
        return bridge.get_snapshot();
    }
    return window.__MOCK_SNAPSHOT__ || { generated_at: '', sessions: [] };
}

/* The HTML-safety primitives. They live in logic.js (pure, so the Node test
   suite covers them) and are aliased here because the markup builders across
   the controller use them densely: `esc` for a value in text position, `attr`
   for a whole attribute - `attr` owns the quoting, so a call site cannot pick a
   quoting style that escaping does not cover. Every interpolated value in the
   controller goes through one of the two, numbers included; the single
   exception is reportUiError above, which escapes inline so it still works when
   logic.js is the thing that failed to load. See the contract in logic.js. */
/* `var`, not `const`: logic.js and the controller scripts load as classic
   scripts sharing one global scope, where logic.js's top-level function
   declarations already created non-configurable `esc`/`attr`/`ansiToHtml`
   globals. A lexical declaration of the same name is a SyntaxError at script
   instantiation - this file would never execute a single statement (empty UI, nothing logged, since the error
   bridge lives in this very file). A `var` redeclaration of a function-backed
   global is permitted; tests/js/global-scope.test.js guards this. */
var esc = logic.esc;
var attr = logic.attr;
var ansiToHtml = logic.ansiToHtml;

function fmt(template, values) {
    return String(template || '').replace(/\{(\w+)\}/g, (_, key) => (values[key] != null ? values[key] : ''));
}

// Local calendar date as YYYY-MM-DD, used to pick the active price schedule.
function todayIso() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    return `${now.getFullYear()}-${month}-${day}`;
}
