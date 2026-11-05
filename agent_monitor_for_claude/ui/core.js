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
    // How the list is grouped: 'urgency' (the sections) or 'project'.
    group: 'urgency',
    // The session the detail pane shows, by its list key (session id + pid). It
    // belongs to the app rather than to the list, so another view can share it.
    // detailOpen is the narrow layout's drill-in: the detail replaces the list.
    selectedKey: null,
    detailOpen: false,
    // One of the selected session's subagents, by agent id, when the graph's
    // node for it was clicked: the detail then describes that agent instead.
    selectedAgent: null,
    // Likewise one of its background processes, by pid.
    selectedProcess: null,
    // A tool of the selected subagent, by its key (a tool name, or mcp:<server>):
    // its activity then lists that tool's calls alone.
    selectedTool: null,
    // Set by a tool picked in the graph: the detail scrolls to the agent's
    // activity once it is loaded.
    scrollToActivity: false,
    // Whether the details render the Markdown of a reply, a plan or a task, or
    // show it as written (amc-markdown).
    markdown: true,
    // The list pane's width per grouping ('list', 'graph') the splitter set, in
    // pixels; a grouping without one keeps the layout's default (amc-list-width).
    listWidths: {},
    // The list entries in display order, and each one's session, as of the last
    // render - for the keyboard navigation and the detail pane.
    listKeys: [],
    sessionsByKey: new Map(),
    // The keys of the sessions blocked on you at the last taskbar report; null
    // before the first, which sets the count without flashing.
    attentionKeys: null,
    // Project groups the user collapsed, by group key.
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
    // How far back the graph reaches (a logic.GRAPH_RANGES key).
    graphRange: logic.DEFAULT_GRAPH_RANGE,
    // The compact view: the narrow, always-on-top window (see header.js).
    compact: false,
};

// Interval (ms) for the cheap change-fingerprint probe between full polls.
const FINGERPRINT_INTERVAL = 1000;

// English fallbacks so the UI never renders empty text, even if the
// bridge delivers its translations late.
const DEFAULT_LABELS = {
    app_title: 'Agent Monitor for Claude',
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
    host_wsl_tip: 'This agent runs inside the WSL distribution “{distro}”. Its files are read over \\\\wsl.localhost; nothing is ever executed inside the distribution.',
    cli_changelog_tip: 'Claude Code {version} - opens the changelog for this version in your browser',
    empty_state: 'No active Claude Code agents.',
    empty_filter: 'No agents match this filter.',
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
    auto_paused: 'Auto mode paused - Claude Code is asking you again',
    auto_denials: 'Calls blocked by the classifier: {count}',
    worktree_tip: 'Works in a git worktree',
    worktree_branch: 'Branch: {branch}',
    branch_tip: 'Git branch: {branch}',
    pr_tip: 'Pull requests linked to this session',
    cache_warm_tip: 'Prompt cache warm for another {time}',
    cache_cold_tip: 'Prompt cache expired - the next prompt writes {tokens} tokens again ({cost})',
    cache_cold_tokens_tip: 'Prompt cache expired - the next prompt writes {tokens} tokens again',
    compactions: 'Context compactions: {count}',
    files_edited: 'Files edited: {count}',
    artifacts: 'Artifacts: {count}',
    artifacts_label: 'Artifacts',
    cost_main: 'Main conversation: {cost}',
    cost_agents: 'Subagents: {cost}',
    cost_agent_unknown: 'other',
    cost_skills: 'By skill:',
    cost_web_search: 'Web searches: {count} · {cost}',
    plan_tip: 'Plan: step {step} of {total}',
    queued_tip: 'Queued messages: {count}',
    budget_tip: 'Budget: {used} of {total} used',
    cli_update_tip: 'Claude Code {available} is available - this agent still runs {running}. Restart it to switch.',
    hook_errors_tip: 'Hooks failing without blocking - check your hook configuration',
    hook_exit_code: 'Exit {code}',
    remote_control_tip: 'Remote Control is on - continue this agent from claude.ai or the Claude app',
    subagents_running: 'Running subagents: {count}',
    subagents_finished: 'Recently finished: {count}',
    subagents_workflow: 'Workflow: {done}/{total} agents',
    processes_running: 'Background processes: {count}',
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
    copy_session_id: 'Copy session ID',
    copied: 'Copied to clipboard',
    show_in_file_manager: 'Show in file manager',
    project_this_computer: 'This computer',
    show_transcript: 'Show transcript in file manager',
    show_scratchpad: 'Show scratchpad in file manager',
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
    search_placeholder: 'Title, project, branch, ID or content',
    section_needs: 'Needs you',
    section_stopped: 'Stopped',
    section_running: 'Running',
    section_recent: 'Recent',
    section_older: 'Older',
    group_by: 'Group by',
    group_urgency: 'Urgency',
    group_project: 'Project',
    group_graph: 'Graph',
    summary_active: 'Active: {count}',
    compact: 'Compact',
    compact_tip: 'Compact view: a narrow window that stays above all other windows',
    compact_exit: 'Full view',
    compact_exit_tip: 'Back to the full window with the session details',
    signal_processes: 'Processes: {count}',
    signal_agents: 'Agents: {count}',
    signal_workflow: 'Workflow {done}/{total}',
    signal_context: 'Context {percent}%',
    signal_budget: 'Budget {percent}%',
    signal_cache: 'Cache expired · {cost}',
    signal_auto_paused: 'Auto mode paused',
    signal_update: 'Update {version}',
    signal_hook_errors: 'Hook errors: {count}',
    signal_queued: 'Queued: {count}',
    search_match_title: 'in title',
    search_match_project: 'in project',
    search_match_branch: 'in branch',
    search_match_id: 'session ID',
    detail_empty: 'Select a session to see its details.',
    detail_back: 'List',
    action_open_window: 'Switch to window',
    action_copy_id: 'Copy ID',
    action_copy_attach: 'Copy attach command',
    action_transcript: 'Show transcript',
    action_scratchpad: 'Show scratchpad',
    action_delete: 'Delete…',
    action_show_details: 'Show details',
    fact_host: 'Host',
    fact_model: 'Model',
    fact_effort: 'Effort',
    fact_mode: 'Mode',
    fact_cli: 'Claude Code',
    fact_speed: 'Speed',
    fact_worktree: 'Worktree',
    fact_branch: 'Branch',
    remote_control_on: 'Remote Control',
    kpi_cost: 'Cost',
    kpi_tokens: 'Tokens',
    kpi_context: 'Context',
    kpi_context_of: '{percent}% of {size}',
    kpi_processes: 'Processes',
    kpi_agents: 'Subagents',
    kpi_plan: 'Plan',
    kpi_budget: 'Budget',
    kpi_cache: 'Prompt cache',
    kpi_cache_warm: 'warm',
    kpi_cache_cold: 'expired',
    kpi_lines: 'Lines changed',
    block_processes: 'Tasks & processes',
    block_agents: 'Subagents',
    block_cost: 'Cost',
    block_session: 'Session',
    block_hits: 'Search hits',
    activity_plan: 'Plan to review',
    kpi_files_sub: 'Files: {count}',
    kpi_work: 'Worked (24 h)',
    kpi_work_sub: 'Waited {wait} · turns: {turns}',
    kpi_work_tip: 'When the session worked, over the last {span}',
    block_waiting: 'Waiting for you',
    block_running: 'Running now',
    block_reply: 'Last answer',
    block_plan: 'Plan',
    block_git: 'Uncommitted changes',
    git_ahead: 'ahead: {count}',
    git_behind: 'behind: {count}',
    git_clean: 'Nothing uncommitted',
    git_summary: 'Changed: {changed} · new: {untracked}',
    git_edited: 'Edited by this session and not committed: {uncommitted} of {total}',
    git_edited_tip: 'Edited by this session',
    tests_passed: 'passed: {count}',
    tests_failed: 'failed: {count}',
    tests_skipped: 'skipped: {count}',
    graph_messages: 'Messages: {count}',
    graph_workflow: 'Workflow',
    graph_peer: 'Outside this list',
    agent_running: 'Running',
    agent_finished: 'Finished',
    fact_agent_type: 'Type',
    fact_skill: 'Skill',
    fact_launch: 'Started as',
    launch_foreground: 'Foreground',
    launch_background: 'Background',
    launch_skill: 'Forked skill',
    fact_started_by: 'Started by',
    kpi_runtime: 'Runtime',
    kpi_runtime_sub: 'Started {time}',
    kpi_tool_calls: 'Tool calls',
    block_task: 'Task',
    block_result: 'Result',
    splitter_tip: 'Drag to resize the list. Double-click to reset.',
    block_activity: 'Activity',
    activity_note: 'Tool calls: {count}',
    step_request: 'Request',
    step_error: 'Error',
    step_reply: 'Reply',
    graph_tool_calls: 'Calls: {count}',
    graph_mcp: 'MCP server',
    graph_tool_failed: 'Failed: {count}',
    step_exit: 'Exit {code}',
    step_rejected: 'Rejected',
    step_interrupted: 'Interrupted',
    md_formatted: 'Formatted',
    md_plain: 'Text',
    fact_pid: 'PID',
    fact_parent_process: 'Parent process',
    session_id_label: 'Session ID',
    model_history_label: 'Models',
    cli_history_label: 'Claude Code versions',
    pr_label: 'Pull requests',
    hook_errors_label: 'Hook errors',
    key_ctrl: 'Ctrl',
};

// One filter bucket per status band. The list sections (logic.LIST_SECTIONS)
// open and close them in groups; a bucket's tip explains its status, and a
// section's tooltip is the tips of the buckets it holds.
const FILTER_DEFS = [
    { key: 'needs', tip: 'filter_needs_tip' },
    { key: 'errored', tip: 'filter_errored_tip' },
    { key: 'interrupted', tip: 'filter_interrupted_tip' },
    { key: 'new', tip: 'filter_new_tip' },
    { key: 'idle', tip: 'filter_idle_tip' },
    { key: 'working', tip: 'filter_working_tip' },
    { key: 'background', tip: 'filter_background_tip' },
    { key: 'quiet', tip: 'filter_quiet_tip' },
    // Off by default: opening "Older" triggers the on-demand scan of past
    // sessions. The key stays `history` because it is part of the persisted
    // filter selection (and of is_history / get_history).
    { key: 'history', tip: 'filter_history_tip', offByDefault: true },
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
        // Every subagent with its tools only while the graph shows them.
        return bridge.get_snapshot(state.group === 'graph' && !state.compact);
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
