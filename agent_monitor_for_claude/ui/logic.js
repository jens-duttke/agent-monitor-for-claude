'use strict';

/* Agent Monitor for Claude - pure presentation logic.

   This module holds every derivation the UI performs on the raw session
   records the Python backend provides: status classification, label
   formatting, grouping and sorting. It has no DOM or bridge dependency, so it
   runs unchanged in the browser (as window.AMC_LOGIC) and under Node for the
   test suite (tests/js). The Python side is a pure data provider; all of the
   logic below used to live there and was ported here verbatim. */

/* --- small helpers --- */

function fmt(template, values) {
    return String(template == null ? '' : template).replace(/\{(\w+)\}/g, (_, key) => (values[key] != null ? values[key] : ''));
}

/* --- status classification (ported from the former status.py) --- */

const NEEDS_ATTENTION = new Set(['awaiting_input', 'awaiting_permission', 'interrupted', 'errored']);

// Lower value sorts first: most urgent (blocked on you) down to terminal. The
// order follows what each state asks of you: blocked-on-you first, then the
// session still doing work (foreground, then background), then a stuck turn that
// stopped on an error, then the calm states - a finished-idle turn, an
// interrupted turn (you stopped it, so it owes nothing), and finally the
// terminal ones. This mirrors the filter chip order.
const STATUS_ORDER = {
    awaiting_permission: 0,
    working: 1,
    processing: 2,
    errored: 3,
    awaiting_input: 4,
    interrupted: 5,
    new: 6,
    unknown: 7,
    completed: 8,
};

// The guiding principle is structural, not time-based: the states that mean
// "your turn" are an assistant turn that ended with end_turn (finished) and the
// interrupt marker (you stopped the turn). In every other live state the model
// owes a response and is therefore working - a just-sent user prompt, a
// tool_result it is still reasoning about, or a long silent thinking phase that
// writes nothing to the transcript for minutes.
function classify(raw) {
    if (!raw.alive) {
        return 'completed';
    }
    if (!raw.has_transcript) {
        return 'new';
    }
    // The user interrupted the running turn: control is back with them and the
    // model owes nothing, so this is its own "your turn" state, never working -
    // even when the interrupt left a tool_use unresolved. A time-based rule
    // cannot tell this from a fresh prompt (both are user turns); the fixed
    // interrupt marker, surfaced by the parser as its own kind, is the only
    // reliable signal. It is a distinct status (not folded into awaiting_input)
    // so an abandoned-mid-task session is told apart from a clean finish.
    if (raw.last_entry_kind === 'user_interrupt') {
        return 'interrupted';
    }
    // The last turn stopped on an API error - a usage/session limit, an
    // overload, or a server error. Nothing is running and the model cannot
    // resume on its own (you wait for the limit to reset, switch, or retry), so
    // this is its own state, never the "working" a non-end_turn assistant turn
    // would imply. Checked before the pending-tool rule: the error ended the
    // turn even if it left a tool_use unresolved (like the interrupt above).
    if (raw.last_entry_kind === 'api_error') {
        return 'errored';
    }
    if (raw.pending_tool) {
        return raw.pending_blocking ? 'awaiting_permission' : 'working';
    }
    if (raw.last_entry_kind === 'assistant' && raw.last_stop_reason === 'end_turn') {
        return 'awaiting_input';
    }
    // A local command (a slash or `!` command) ran as the newest turn. It
    // executes outside the model - Claude Code writes a caveat telling the model
    // not to respond - so nothing is owed and the session is idle, not working.
    // (This can briefly read idle while a prompt-style command's first turn is
    // still being thought out, but that is transient, unlike a stuck "working".)
    if (raw.last_entry_kind === 'local_command') {
        return 'awaiting_input';
    }
    if (raw.last_entry_kind === 'user_text' || raw.last_entry_kind === 'tool_result') {
        return 'working';
    }
    if (raw.last_entry_kind === 'assistant') {
        return 'working';
    }
    if (raw.has_activity || raw.last_stop_reason != null) {
        return 'awaiting_input';
    }
    return 'unknown';
}

function refineWithNative(status, nativeStatus, waitingFor) {
    // `interrupted` and `errored` come from a definitive trailing entry (the
    // interrupt marker, or an API-error turn) being the newest - no turn is
    // running, so a lagging registry `busy`/`idle` must not flip or flatten
    // them. Guarded like awaiting_permission.
    if (status === 'awaiting_permission' || status === 'interrupted' || status === 'errored' || nativeStatus == null) {
        return status;
    }
    if (nativeStatus === 'busy' && status !== 'new') {
        return 'working';
    }
    // "waiting" with a reason means Claude Code is blocked on an interactive
    // prompt (e.g. a permission request) whose tool_use has not yet reached the
    // transcript - so the structural rule still sees the user's own prompt and
    // reads "working". The registry is authoritative: the agent is blocked on
    // you and cannot proceed, which is awaiting_permission, not the calmer
    // "finished, optional" awaiting_input.
    if (nativeStatus === 'waiting' && waitingFor && status !== 'new') {
        return 'awaiting_permission';
    }
    if (nativeStatus === 'idle' && status !== 'new') {
        return 'awaiting_input';
    }
    return status;
}

// Running subagents, or a background OS process the session started, mean it is
// busy even if its own transcript has gone quiet. Only the idle-looking states
// are promoted; a working agent or a user-blocking dialog is left untouched.
function refineWithBackgroundWork(status, backgroundWork) {
    if (!backgroundWork) {
        return status;
    }
    // An interrupted session whose background work is a still-running OS child
    // (subagents are already excluded upstream, as they die with the interrupt)
    // has real work going, so it reads as processing rather than interrupted.
    if (status === 'awaiting_input' || status === 'unknown' || status === 'interrupted') {
        return 'processing';
    }
    return status;
}

function needsAttention(status) {
    return NEEDS_ATTENTION.has(status);
}

// Which toolbar filter chip a status belongs to. One chip per status color, so
// the chips double as the status legend.
const STATUS_FILTER = {
    awaiting_permission: 'needs',
    errored: 'errored',
    interrupted: 'interrupted',
    awaiting_input: 'idle',
    working: 'working',
    processing: 'background',
    completed: 'quiet',
    unknown: 'quiet',
    new: 'new',
};

function filterBucket(status) {
    return STATUS_FILTER[status] || null;
}

// Which filter chip a whole session belongs to. A past (history) session is
// always non-live and would otherwise fall under "quiet" like any completed
// one; it gets its own "history" bucket instead so the on-demand history
// listing has a dedicated, off-by-default chip separate from the recently-ended
// sessions the live snapshot still carries.
function sessionBucket(session) {
    if (session && session.is_history) {
        return 'history';
    }
    return filterBucket(session ? session.status : null);
}

// The history cache is deduped against the live registry only once, at fetch
// time. A past session that is resumed comes back into the live snapshot, so
// folding the (now stale) cached history in as well would render it twice - a
// live row plus a dimmed, undeletable history row. Drop any cached history
// record whose session is currently live before folding it in.
function pruneResumedHistory(historyRecords, liveSessions) {
    if (!Array.isArray(historyRecords) || historyRecords.length === 0) {
        return [];
    }
    const liveIds = new Set();
    for (const session of liveSessions || []) {
        if (session && session.session_id) {
            liveIds.add(session.session_id);
        }
    }
    return historyRecords.filter((record) => record && !liveIds.has(record.session_id));
}

// Whether the cached history is stale because a session that was live has left
// the snapshot (it ended and its registry record was pruned). The one-shot
// history fetch had excluded it as live, so without a re-fetch it would vanish
// from both views. True when any previously-present session id is now gone.
function historyNeedsRefresh(previousSessions, currentSessions) {
    if (!Array.isArray(previousSessions) || previousSessions.length === 0) {
        return false;
    }
    const currentIds = new Set();
    for (const session of currentSessions || []) {
        if (session && session.session_id) {
            currentIds.add(session.session_id);
        }
    }
    return previousSessions.some((session) => session && session.session_id && !currentIds.has(session.session_id));
}

// Full status for one raw record, combining the transcript-derived state with
// the registry's busy/idle field and any background work.
function deriveStatus(raw) {
    const toolRunning = (raw.child_count || 0) > 0;
    const pendingBlocking = Boolean(raw.pending_tool) && !toolRunning
        && pendingIsBlocking(raw.last_tool_name, raw.permission_mode);

    let status = classify({
        alive: raw.alive,
        has_transcript: raw.has_transcript,
        last_stop_reason: raw.last_stop_reason,
        pending_tool: raw.pending_tool,
        pending_blocking: pendingBlocking,
        has_activity: raw.has_activity,
        last_entry_kind: raw.last_entry_kind,
    });

    if (raw.alive) {
        status = refineWithNative(status, raw.native_status, raw.waiting_for);
        // A force-stopped turn (an interrupt, or an API error such as a usage
        // limit) tears down in-process subagents, so a still-"running" count is
        // a phantom until the recent window clears it - it must not promote the
        // session to "processing". A detached OS child process (a build or
        // server) can outlive the stop, so it still counts.
        const turnStopped = raw.last_entry_kind === 'user_interrupt' || raw.last_entry_kind === 'api_error';
        const subagentsRunning = turnStopped ? 0 : (raw.subagents_running || 0);
        const backgroundWork = subagentsRunning > 0 || toolRunning;
        status = refineWithBackgroundWork(status, backgroundWork);
    }
    return status;
}

/* --- pending-tool blocking + mode (ported from formatting.py) --- */

const QUESTION_TOOLS = new Set(['AskUserQuestion']);
const PLAN_TOOLS = new Set(['ExitPlanMode', 'EnterPlanMode']);
const DIALOG_TOOLS = new Set(['AskUserQuestion', 'ExitPlanMode', 'EnterPlanMode']);
const PROMPTING_MODES = new Set(['default']);

const MODE_LABELS = {
    default: 'Manual',
    acceptEdits: 'Auto-edit',
    auto: 'Auto',
    plan: 'Plan',
    bypassPermissions: 'Bypass',
};

function pendingIsBlocking(toolName, permissionMode) {
    if (DIALOG_TOOLS.has(toolName)) {
        return true;
    }
    return PROMPTING_MODES.has(permissionMode);
}

function modeLabel(permissionMode) {
    if (!permissionMode) {
        return null;
    }
    return MODE_LABELS[permissionMode] || permissionMode;
}

/* --- label formatting (ported from formatting.py) --- */

function statusLabel(status, labels) {
    return labels['status_' + status] || status;
}

function attentionLabel(status, pendingToolName, labels, usageLimited) {
    if (status === 'awaiting_permission') {
        if (QUESTION_TOOLS.has(pendingToolName)) {
            return labels.status_question;
        }
        if (PLAN_TOOLS.has(pendingToolName)) {
            return labels.status_plan_review;
        }
        // Blocked on you, but with no pending tool in the transcript to say what
        // for - the registry-derived "waiting" case. We cannot tell a permission
        // request from a plain question here, so the label stays neutral rather
        // than claiming "permission needed". A known tool name still means a real
        // permission prompt and keeps the specific label below.
        if (!pendingToolName) {
            return labels.status_needs_you;
        }
    }
    // A stuck-on-error session names the usage/session limit specifically (the
    // common, actionable case - wait for the reset); any other API error keeps
    // the generic label.
    if (status === 'errored') {
        return usageLimited ? labels.status_usage_limit : statusLabel(status, labels);
    }
    return statusLabel(status, labels);
}

function formatAge(seconds, labels) {
    if (seconds == null) {
        return '';
    }
    const total = Math.max(0, Math.floor(seconds));
    if (total < 60) {
        return fmt(labels.age_seconds, { s: total });
    }
    const minutes = Math.floor(total / 60);
    if (minutes < 60) {
        return fmt(labels.age_minutes, { m: minutes });
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
        return fmt(labels.age_hours, { h: hours, m: minutes % 60 });
    }
    return fmt(labels.age_days, { d: Math.floor(hours / 24) });
}

// Turn an API model id into a short label without a mapping table, e.g.
// "claude-opus-4-8[1m]" -> "Opus 4.8 1M", "claude-fable-5" -> "Fable 5".
function formatModel(modelId) {
    if (!modelId) {
        return null;
    }

    let base = modelId;
    let bracket = '';
    if (base.endsWith(']') && base.includes('[')) {
        const open = base.lastIndexOf('[');
        bracket = ' ' + base.slice(open + 1, -1).toUpperCase();
        base = base.slice(0, open);
    }

    if (base.startsWith('claude-')) {
        base = base.slice('claude-'.length);
    }

    const words = [];
    const numbers = [];
    for (const part of base.split('-')) {
        if (/^[a-zA-Z]+$/.test(part)) {
            words.push(part.charAt(0).toUpperCase() + part.slice(1).toLowerCase());
        } else if (/^\d+$/.test(part) && part.length < 4) {
            numbers.push(part);
        }
    }

    if (words.length === 0) {
        return modelId;
    }

    let label = words.join(' ');
    if (numbers.length) {
        label += ' ' + numbers.join('.');
    }
    return label + bracket;
}

function formatTokens(count) {
    const value = count || 0;
    if (value < 1000) {
        return String(value);
    }
    if (value < 1000000) {
        const thousands = value / 1000;
        return thousands >= 100 ? Math.round(thousands) + 'k' : thousands.toFixed(1) + 'k';
    }
    return (value / 1000000).toFixed(1) + 'M';
}

function tokenLabels(usage, labels) {
    const source = usage || {};
    const input = source.input_tokens || 0;
    const output = source.output_tokens || 0;
    const cacheRead = source.cache_read_input_tokens || 0;
    const write5m = source.cache_creation_5m_input_tokens || 0;
    const write1h = source.cache_creation_1h_input_tokens || 0;
    // Writes not attributed to a TTL (older turns without the split) are shown
    // as a combined "cache write" so nothing silently drops off the total.
    const writeOther = Math.max(0, (source.cache_creation_input_tokens || 0) - write5m - write1h);

    if (!(input || output || cacheRead || write5m || write1h || writeOther)) {
        return '';
    }

    // Everything lives on the single visible line (no tooltip), mirroring the
    // pricing tiers: base input, output, cache hits, then cache writes split by
    // TTL. Only the cache parts that occur are shown, to keep the line compact.
    const values = {
        input: formatTokens(input),
        output: formatTokens(output),
        cache_read: formatTokens(cacheRead),
        cache_5m: formatTokens(write5m),
        cache_1h: formatTokens(write1h),
        cache_write: formatTokens(writeOther),
    };

    let summary = fmt(labels.token_summary, values);
    if (cacheRead > 0) {
        summary += ' · ' + fmt(labels.token_cache_read, values);
    }
    if (write5m > 0) {
        summary += ' · ' + fmt(labels.token_cache_5m, values);
    }
    if (write1h > 0) {
        summary += ' · ' + fmt(labels.token_cache_1h, values);
    }
    if (writeOther > 0) {
        summary += ' · ' + fmt(labels.token_cache_write, values);
    }
    return summary;
}

/* --- token cost estimation ---

   Prices come entirely from the hand-maintained pricing.json (shipped via the
   bootstrap bridge, mock-supplied in dev). This module never hardcodes a rate;
   it only resolves the schedule current for a given date and multiplies token
   counts by the per-model rates. Each rate is US dollars per million tokens
   with explicit fields (input, output, cache_read, cache_write_5m,
   cache_write_1h) - no multipliers. A model absent from the schedule has no
   rate, so a session touching it shows a plain token total instead of a wrong
   price. The 1M-context tier is not modelled - long-context turns are
   undercounted. */
const TOKENS_PER_UNIT_PRICE = 1000000;

// Pick the price schedule in effect on `dateStr` (ISO YYYY-MM-DD): the entry
// with the latest date on or before it. ISO dates compare correctly as strings.
function resolvePrices(schedules, dateStr) {
    if (!schedules || typeof schedules !== 'object') {
        return {};
    }
    let best = null;
    for (const date of Object.keys(schedules)) {
        if (date <= dateStr && (best === null || date > best)) {
            best = date;
        }
    }
    return best === null ? {} : (schedules[best] || {});
}

// Map a model id to its pricing.json key: drop "claude-", any "[tier]" and a
// trailing snapshot date, leaving family-version, e.g. "claude-opus-4-8[1m]" ->
// "opus-4-8", "claude-haiku-4-5-20251001" -> "haiku-4-5".
function modelPriceKey(modelId) {
    if (!modelId) {
        return null;
    }
    let key = String(modelId);
    const bracket = key.indexOf('[');
    if (bracket !== -1) {
        key = key.slice(0, bracket);
    }
    if (key.startsWith('claude-')) {
        key = key.slice('claude-'.length);
    }
    key = key.replace(/-\d{6,}$/, '');
    return key || null;
}

// Dollar cost of one model's usage at its rate. Cache writes without a TTL
// split are priced at the 5m (default) write rate.
function usageCostUsd(usage, rate) {
    const source = usage || {};
    const write5m = source.cache_creation_5m_input_tokens || 0;
    const write1h = source.cache_creation_1h_input_tokens || 0;
    const writeOther = Math.max(0, (source.cache_creation_input_tokens || 0) - write5m - write1h);

    const dollars =
        (source.input_tokens || 0) * (rate.input || 0)
        + (source.output_tokens || 0) * (rate.output || 0)
        + (source.cache_read_input_tokens || 0) * (rate.cache_read || 0)
        + (write5m + writeOther) * (rate.cache_write_5m || 0)
        + write1h * (rate.cache_write_1h || 0);

    return dollars / TOKENS_PER_UNIT_PRICE;
}

// Total estimated cost across every model the session used, or null if any of
// them has no rate in `prices` (then the UI shows a plain token total instead).
function sessionCostUsd(usageByModel, prices) {
    const models = usageByModel || {};
    const table = prices || {};
    const ids = Object.keys(models);
    if (ids.length === 0) {
        return null;
    }
    let total = 0;
    for (const modelId of ids) {
        const usage = models[modelId];
        // A model that consumed no tokens adds no cost, so a missing rate for it
        // (e.g. a zero-usage placeholder) must not force the whole session to the
        // token-total fallback.
        if (usageTotalTokens(usage) === 0) {
            continue;
        }
        const key = modelPriceKey(modelId);
        const rate = key ? table[key] : null;
        if (!rate) {
            return null;
        }
        total += usageCostUsd(usage, rate);
    }
    return total;
}

// Whole dollars ("$19"); anything under a dollar is just "<$1". The estimate is
// coarse enough that cents and a "~" would be false precision.
function formatCost(usd) {
    if (usd == null) {
        return null;
    }
    if (usd < 1) {
        return '<$1';
    }
    return '$' + Math.round(usd);
}

function usageTotalTokens(usage) {
    const source = usage || {};
    return (source.input_tokens || 0) + (source.output_tokens || 0)
        + (source.cache_read_input_tokens || 0) + (source.cache_creation_input_tokens || 0);
}

/* --- host / entrypoint (ported from snapshot.py) --- */

const ENTRYPOINT_HOSTS = { 'claude-vscode': 'VS Code' };

function hostLabel(detected, entrypoint) {
    if (detected) {
        return detected;
    }
    if (entrypoint) {
        return ENTRYPOINT_HOSTS[entrypoint] || null;
    }
    return null;
}

function isViaCli(raw) {
    return Boolean(raw.via_cli) || raw.entrypoint === 'cli';
}

function isVscodeDeeplink(raw) {
    return raw.entrypoint === 'claude-vscode';
}

/* --- project grouping (ported from snapshot.py) --- */

function groupKey(cwd) {
    return String(cwd).replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
}

function displayCwd(cwd) {
    if (cwd.length >= 2 && cwd[1] === ':' && /[a-zA-Z]/.test(cwd[0])) {
        return cwd[0].toUpperCase() + cwd.slice(1);
    }
    return cwd;
}

function projectName(cwd) {
    const normalized = String(cwd).replace(/\\/g, '/').replace(/\/+$/, '');
    const segments = normalized.split('/');
    return segments[segments.length - 1] || cwd;
}

// Capability order for the model sort; unknown families sort last.
const MODEL_RANK = ['haiku', 'sonnet', 'opus', 'fable', 'mythos'];

function modelRank(label) {
    if (!label) {
        return MODEL_RANK.length;
    }
    const lowered = label.toLowerCase();
    const rank = MODEL_RANK.findIndex((family) => lowered.includes(family));
    return rank === -1 ? MODEL_RANK.length : rank;
}

/* --- view assembly --- */

// The main conversation's model-switch timeline, oldest first: one entry per
// contiguous run of a model, each carrying the moment that run began. Already
// ordered and run-compressed by the backend, so a model left and returned to
// appears more than once and the last entry is the current model. Feeds the
// model column's "(+)" history when more than one run occurred. Timestamps stay
// raw ISO - the UI formats them.
function modelHistory(timeline) {
    const entries = Array.isArray(timeline) ? timeline : [];
    return entries.map((entry) => ({ time: entry.time, label: formatModel(entry.model) }));
}

// Turn one raw backend record into the display object the renderer consumes.
// Everything here is derived; age is kept numeric so the UI can tick it live.
function buildSession(raw, labels, prices) {
    const status = deriveStatus(raw);
    const toolRunning = (raw.child_count || 0) > 0;
    const usage = raw.usage || {};
    const models = modelHistory(raw.model_timeline);

    // In-process subagents die when the turn is force-stopped - by an interrupt
    // or an API error (a usage limit stops the whole CLI) - so any still-"running"
    // count is a phantom the recent window has yet to clear. Hide it here too, so
    // the row does not show a running subagent next to a stopped status.
    const turnStopped = raw.last_entry_kind === 'user_interrupt' || raw.last_entry_kind === 'api_error';
    const subagentsRunning = turnStopped ? 0 : (raw.subagents_running || 0);
    const subagentsLabels = turnStopped ? [] : (raw.subagents_labels || []);

    // Two parts so the row can animate the reveal: a compact anchor shown by
    // default (the cost when it can be priced, else a plain token total so the
    // number is never wrong), and the per-category breakdown that slides open
    // before it on hover. Expanded reads "<breakdown> · <anchor>".
    const breakdown = tokenLabels(raw.usage, labels);
    const costText = formatCost(sessionCostUsd(raw.usage_by_model, prices)) || '';
    let usageCompact = '';
    let usageDetail = '';
    if (breakdown) {
        usageCompact = costText || formatTokens(usageTotalTokens(usage));
        // The separator's trailing space sits at the end of .usage-detail (an
        // overflow:hidden flex item), where a normal space is stripped as
        // trailing whitespace and the compact anchor would butt against the dot.
        // A non-breaking space is not collapsed, so " · " keeps its gap; it is
        // written as an explicit unicode escape (not a literal nbsp
        // character) so no whitespace-normalizing tool can silently turn it
        // back into a plain space.
        usageDetail = breakdown + ' ·\u00A0';
    }

    return {
        session_id: raw.session_id,
        pid: raw.pid,
        cwd: raw.cwd,
        is_history: Boolean(raw.is_history),
        name: raw.title || raw.short_name,
        title: raw.title || '',
        short_name: raw.short_name,
        kind: raw.kind,
        status: status,
        // Only name the tool when one is actually pending; last_tool_name lingers
        // from a resolved tool, so the registry-`waiting` route (no pending tool)
        // must fall through to the neutral label - matching the deriveStatus gate.
        status_label: attentionLabel(status, raw.pending_tool ? raw.last_tool_name : null, labels, raw.usage_limited),
        needs_attention: needsAttention(status),
        model: formatModel(raw.model_id),
        model_switched: models.length > 1,
        model_history: models,
        usage_compact: usageCompact,
        usage_detail: usageDetail,
        usage_total: usageTotalTokens(usage),
        subagents_running: subagentsRunning,
        subagents_done: raw.subagents_done || 0,
        subagents_labels: subagentsLabels,
        processes: raw.child_count || 0,
        process_names: raw.child_names || [],
        tool_running: toolRunning,
        host: hostLabel(raw.host, raw.entrypoint),
        via_cli: isViaCli(raw),
        mode: modeLabel(raw.permission_mode),
        vscode_deeplink: isVscodeDeeplink(raw),
        age_seconds: raw.age_seconds == null ? null : Math.floor(raw.age_seconds),
    };
}

/* --- project ordering (cross-project attention bands) --- */

// Projects are ordered by attention band, not by fine-grained status. Within a
// band the order is a stable alphabetical sort, so a panel only moves when it
// crosses a boundary that actually changes its relevance to you: blocked first,
// then busy, then quiet. Fine-grained churn inside a band (working <->
// processing, a ticking token count) never reorders the panels.
//
// Only a session actually blocked on you (awaiting_permission - a question,
// plan review, or permission prompt that cannot proceed without an answer) sits
// in the top band. A finished "your turn" (awaiting_input) is deliberately
// quiet, alongside new/finished sessions: nothing is running and nothing is
// mandatory, so it sinks below the projects that are still doing work. Without
// this, a session ending its turn would land in the top band with the truly
// blocked ones, and since most idle sessions read as awaiting_input the whole
// order would collapse to alphabetical.
const QUIET_BAND = 2;

const STATUS_BAND = {
    awaiting_permission: 0,
    working: 1,
    processing: 1,
    errored: QUIET_BAND,
    interrupted: QUIET_BAND,
    awaiting_input: QUIET_BAND,
    new: QUIET_BAND,
    unknown: QUIET_BAND,
    completed: QUIET_BAND,
};

// A project is as urgent as its most urgent session (its lowest band).
function projectBand(sessions) {
    let band = QUIET_BAND;
    for (const session of sessions || []) {
        const value = STATUS_BAND[session.status];
        if (value != null && value < band) {
            band = value;
        }
    }
    return band;
}

function compareProjectsByName(a, b) {
    const nameA = String(a.name || '').toLowerCase();
    const nameB = String(b.name || '').toLowerCase();
    if (nameA !== nameB) {
        return nameA < nameB ? -1 : 1;
    }
    const cwdA = String(a.cwd || '').toLowerCase();
    const cwdB = String(b.cwd || '').toLowerCase();
    if (cwdA !== cwdB) {
        return cwdA < cwdB ? -1 : 1;
    }
    return 0;
}

// Order projects for display. With byPriority, projects are grouped into
// attention bands (needs-you, busy, quiet) and sorted alphabetically within
// each; otherwise the list is a plain alphabetical one. Both are stable - the
// order changes only when a project crosses a band boundary, never on token or
// fine-grained status churn.
function sortProjects(projects, byPriority) {
    const ordered = [...(projects || [])];
    if (!byPriority) {
        return ordered.sort(compareProjectsByName);
    }
    return ordered.sort((a, b) => {
        const bandA = projectBand(a.sessions);
        const bandB = projectBand(b.sessions);
        if (bandA !== bandB) {
            return bandA - bandB;
        }
        return compareProjectsByName(a, b);
    });
}

// Group raw records into projects (case-insensitively, like Windows paths).
function groupProjects(rawSessions, labels, prices) {
    const groups = new Map();
    for (const raw of rawSessions || []) {
        const key = groupKey(raw.cwd);
        let group = groups.get(key);
        if (!group) {
            group = { cwd: displayCwd(raw.cwd), name: projectName(raw.cwd), sessions: [] };
            groups.set(key, group);
        }
        group.sessions.push(buildSession(raw, labels, prices));
    }
    return [...groups.values()];
}

const AMC_LOGIC = {
    fmt,
    classify,
    deriveStatus,
    refineWithNative,
    refineWithBackgroundWork,
    needsAttention,
    filterBucket,
    sessionBucket,
    pruneResumedHistory,
    historyNeedsRefresh,
    pendingIsBlocking,
    modeLabel,
    statusLabel,
    attentionLabel,
    formatAge,
    formatModel,
    formatTokens,
    tokenLabels,
    resolvePrices,
    modelPriceKey,
    usageCostUsd,
    sessionCostUsd,
    formatCost,
    usageTotalTokens,
    modelHistory,
    hostLabel,
    isViaCli,
    isVscodeDeeplink,
    groupKey,
    displayCwd,
    projectName,
    modelRank,
    buildSession,
    groupProjects,
    projectBand,
    sortProjects,
    STATUS_ORDER,
    STATUS_BAND,
    STATUS_FILTER,
    MODEL_RANK,
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = AMC_LOGIC;
}
if (typeof window !== 'undefined') {
    window.AMC_LOGIC = AMC_LOGIC;
}
