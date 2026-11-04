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

/* --- HTML safety ---

   The UI builds its markup by string concatenation, so every interpolated value
   has to pass through one of exactly two primitives: `esc` in text position,
   `attr` for a whole attribute. They live here, not in the controller, because they
   are pure - that is what puts them under the Node test suite, where the
   guarantee below is asserted rather than assumed.

   `esc` escapes the five characters that can leave text position or a quoted
   attribute value, in a single pass - a chained-replace version silently
   double-escapes if the `&` step is ever reordered. It covers text and any
   quoted attribute; it is NOT sufficient in an unquoted attribute, in a
   URL-bearing attribute (`href`/`src`, where a `javascript:` value needs no
   metacharacter at all), or inside `<style>`/`<script>` (the page's CSP
   forbids both anyway). The backtick is deliberately not escaped: it only ever
   delimited attributes in IE < 10, and the sole target here is a modern
   WebView2/Chromium.

   Prefer `attr`: it owns the quotes, so a call site cannot pick a quoting style
   the escaping does not cover, and that choice never has to be re-audited when
   `esc` changes. Escaping the value alone leaves that decision at the call site,
   where a wrong one still reads as escaped at a glance. */
const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

const ATTR_NAME = /^[a-zA-Z][a-zA-Z0-9-]*$/;

function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

// One double-quoted attribute, leading space included, ready to concatenate
// into a tag. The name has to be a literal in the code: a name built from data
// would be markup injection that no amount of value escaping can catch, so a
// name that is not a plain HTML identifier is a programming error and throws
// (surfaced by reportUiError) rather than emitting half an attribute.
function attr(name, value) {
    if (!ATTR_NAME.test(String(name))) {
        throw new TypeError('attr: attribute name must be a literal identifier, got ' + String(name));
    }
    return ' ' + name + '="' + esc(value) + '"';
}

/* --- terminal output rendering --- */

// ANSI SGR foreground codes -> themed CSS class. Backgrounds and other
// attributes are intentionally not mapped (kept simple and legible).
const ANSI_FG = {
    30: 'ansi-black', 31: 'ansi-red', 32: 'ansi-green', 33: 'ansi-yellow',
    34: 'ansi-blue', 35: 'ansi-magenta', 36: 'ansi-cyan', 37: 'ansi-white',
    90: 'ansi-bright-black', 91: 'ansi-bright-red', 92: 'ansi-bright-green', 93: 'ansi-bright-yellow',
    94: 'ansi-bright-blue', 95: 'ansi-bright-magenta', 96: 'ansi-bright-cyan', 97: 'ansi-bright-white',
};

// Render a background task's output as safe HTML: escape every text run, turn
// ANSI SGR color codes into themed spans, and drop the other control sequences
// a non-emulating console cannot honor (cursor moves, line erases). This is the
// one place untrusted process output reaches markup, so only fixed class names
// from ANSI_FG are ever emitted - never any part of the raw code - and every
// text run goes through `esc`.
function ansiToHtml(value) {
    const text = String(value == null ? '' : value);
    const csi = /\x1b\[[0-9;?]*[A-Za-z]/g;
    let html = '';
    let index = 0;
    let fg = null;
    let bold = false;

    const flush = (segment) => {
        if (!segment) {
            return;
        }
        const escaped = esc(segment);
        const classes = [];
        if (fg) {
            classes.push(fg);
        }
        if (bold) {
            classes.push('ansi-bold');
        }
        html += classes.length ? '<span' + attr('class', classes.join(' ')) + '>' + escaped + '</span>' : escaped;
    };

    let match;
    while ((match = csi.exec(text)) !== null) {
        flush(text.slice(index, match.index));
        index = csi.lastIndex;
        const seq = match[0];
        if (seq[seq.length - 1] !== 'm') {
            continue;   // a cursor/erase control, not a color - drop it
        }
        const body = seq.slice(2, -1);
        const params = body === '' ? ['0'] : body.split(';');
        for (const param of params) {
            const code = parseInt(param, 10);
            if (code === 0) {
                fg = null;
                bold = false;
            } else if (code === 1) {
                bold = true;
            } else if (code === 22) {
                bold = false;
            } else if (code === 39) {
                fg = null;
            } else if (ANSI_FG[code]) {
                fg = ANSI_FG[code];
            }
        }
    }
    flush(text.slice(index));
    return html;
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
    // The newest turn was written before the process now holding the session
    // started: the session was reopened after an earlier process ended. Nothing
    // that process began is still running - the reply it owed, the tool call it
    // left open or the dialog it showed died with it - and this process has not
    // taken a turn yet, so the session waits for you. Checked before the
    // pending-tool rule for the same reason as the two stops above: a dangling
    // tool_use here is a call that will never be answered, neither executing nor
    // prompting. The two stops themselves stay definitive (an interrupt is still
    // an interrupt after a reopen). See turnPredatesProcess for the comparison.
    if (raw.turn_predates_process) {
        return 'awaiting_input';
    }
    if (raw.pending_tool) {
        return raw.pending_blocking ? 'awaiting_permission' : 'working';
    }
    if (raw.last_entry_kind === 'assistant' && raw.last_stop_reason === 'end_turn') {
        // Claude Desktop ends a turn through its wrap-up call, which says when
        // the agent cannot go on without your decision. That is not the calm
        // "finished, nothing required" of an ordinary end of turn but a block on
        // you, like an open dialog.
        return raw.turn_end_blocked ? 'awaiting_permission' : 'awaiting_input';
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

// The workflow runs the snapshot reports as still active (from each run's
// journal: agents still open, or its journal written within the grace window).
// This is a workflow-level signal, robust to the gap between fan-out phases
// where no single agent is momentarily running - which otherwise flickered the
// whole session between "working" and "your turn".
function activeWorkflows(workflows) {
    if (!Array.isArray(workflows)) {
        return [];
    }
    return workflows.filter((workflow) => workflow && workflow.active);
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

// The content-search scope: exactly the sessions the active filter chips show.
// A live session is in scope only when its status chip is on; history sessions
// only when the history chip is on and includeHistory is set. With
// includeHistory false (the delta rescan), only live, chip-visible sessions are
// returned AND dead ones are skipped: a dead transcript is append-only and not
// growing, so it can never gain a new match - re-reading it every poll is waste
// the delta path must avoid. The initial full search (includeHistory true) still
// reads a dead-but-visible session once.
//
// `includeHidden` is the one deliberate exception, set only after the user
// clicks the empty result's widening offer: the chip filter is lifted off the
// SCOPE (never off the chips themselves), so the scan reaches the sessions the
// chips hide - past ones included, whether or not their chip is on. Everything
// else still holds, the delta rescan's dead-session skip above included.
function searchScopeRefs(sessions, history, filterKeys, includeHistory, includeHidden) {
    const refs = [];
    const seen = new Set();
    const filters = filterKeys instanceof Set ? filterKeys : new Set(filterKeys || []);

    const add = (list, isHistory) => {
        for (const raw of list || []) {
            if (!raw || !raw.session_id || !raw.cwd) {
                continue;
            }
            if (!includeHistory && !raw.alive) {
                continue;
            }
            if (!includeHidden) {
                const bucket = isHistory ? 'history' : filterBucket(deriveStatus(raw));
                if (!bucket || !filters.has(bucket)) {
                    continue;
                }
            }
            const key = raw.session_id + '|' + raw.cwd;
            if (!seen.has(key)) {
                seen.add(key);
                refs.push({ session_id: raw.session_id, cwd: raw.cwd, origin: sessionOrigin(raw) });
            }
        }
    };

    add(sessions, false);
    if (includeHistory && (includeHidden || filters.has('history')) && Array.isArray(history)) {
        add(history, true);
    }
    return refs;
}

// How far back the history listing reaches. A machine that has run Claude Code
// for a while accumulates hundreds of past sessions, of which only the newest
// few are ever the answer to "I just closed that by accident" - so the listing
// is a graded window rather than all-or-nothing. `seconds: null` means no
// bound. The window is passed to the backend, which uses it to skip
// out-of-window transcripts without opening them, so a short window is also a
// much cheaper scan.
const HISTORY_RANGES = [
    { key: '1h', seconds: 3600, label: 'history_range_1h' },
    { key: '24h', seconds: 86400, label: 'history_range_24h' },
    { key: '7d', seconds: 604800, label: 'history_range_7d' },
    { key: '30d', seconds: 2592000, label: 'history_range_30d' },
    { key: 'all', seconds: null, label: 'history_range_all' },
];

// Wide enough to cover "yesterday I was still working on this", narrow enough
// to stay a scannable list on a machine with hundreds of past sessions.
const DEFAULT_HISTORY_RANGE = '24h';

// The range definition for a key, falling back to the default for an unknown
// one (a stale value from localStorage, say) so a bad key can never leave the
// listing without a window.
function historyRange(key) {
    return HISTORY_RANGES.find((range) => range.key === key)
        || HISTORY_RANGES.find((range) => range.key === DEFAULT_HISTORY_RANGE);
}

function historyRangeSeconds(key) {
    return historyRange(key).seconds;
}

// Whether history already fetched for `loadedSeconds` still covers `wantedSeconds`.
// The fetch returns everything within its window, so a *narrower* selection is
// already in hand and only needs filtering - re-scanning would read files the
// cache can answer for. Only widening (or "all", which nothing but itself
// covers) needs the backend again. A never-fetched cache (undefined) covers
// nothing.
function historyRangeCovered(loadedSeconds, wantedSeconds) {
    if (loadedSeconds === undefined) {
        return false;
    }
    if (loadedSeconds === null) {
        return true;
    }
    if (wantedSeconds === null) {
        return false;
    }
    return wantedSeconds <= loadedSeconds;
}

// Narrow cached history records to the selected window. Ages are the ones
// frozen at fetch time (the same values the rows display from), so the set is
// stable while the user reads it instead of shedding rows as they tick past the
// boundary. A record whose age could not be determined is kept: the listing
// errs towards showing a session it cannot date rather than hiding it.
function filterHistoryByAge(records, maxAgeSeconds) {
    if (!Array.isArray(records)) {
        return [];
    }
    if (maxAgeSeconds == null) {
        return records;
    }
    return records.filter((record) => record && (record.age_seconds == null || record.age_seconds <= maxAgeSeconds));
}

// Which widening step an empty search result can still offer, cheapest first.
// 'hidden' pulls in the sessions the chips hide (past ones inside the current
// window included); 'range' reaches one window further back. null means the
// query already covered everything reachable, so "not found" is the whole
// answer and no button should appear - an offer that cannot widen anything
// would just send the user in a circle.
function searchWideningStep(options) {
    const opts = options || {};
    const all = asKeySet(opts.allKeys);
    const active = asKeySet(opts.activeKeys);

    if (!opts.includeHidden) {
        for (const key of all) {
            if (!active.has(key)) {
                return 'hidden';
            }
        }
    }
    if (nextHistoryRange(opts.historyRange) != null) {
        return 'range';
    }
    return null;
}

// The next wider window after `key`, or null once the widest ("all") is
// selected. Drives the second widening step, and answers "is there anything
// further back at all" without duplicating the range order.
function nextHistoryRange(key) {
    const current = historyRange(key).key;
    const index = HISTORY_RANGES.findIndex((range) => range.key === current);
    if (index < 0 || index + 1 >= HISTORY_RANGES.length) {
        return null;
    }
    return HISTORY_RANGES[index + 1].key;
}

// A filter-key collection as a Set, whether the caller had one or an array.
function asKeySet(keys) {
    return keys instanceof Set ? keys : new Set(keys || []);
}

/* --- empty-state and widened-search markup --- */

// The empty content area and the widened-search notice are built here rather
// than in the controller because they are pure string work: they take a plain `view`
// object (assembled once by render.js `emptyStateView`) instead of reading UI
// state, which puts the wording rules - which message wins, whether a widening
// is offered, what the hint names - under tests/js/logic.test.js. The view uses
// the same key names searchWideningStep takes, so the decision is made in one
// place and never restated here.

function emptyBlock(message, hint) {
    return '<div class="empty">' + esc(message || '')
        + (hint ? '<span class="empty-hint">' + esc(hint) + '</span>' : '')
        + '</div>';
}

// The message for an empty content area, in the order the reasons matter. A
// running scan or history load speaks first - the emptiness says nothing yet.
// Then a query that genuinely matched nothing, which gets its own message (and
// its widening offer) rather than the filter one: blaming the chips for a search
// result is what sent people looking in the wrong place. Only then the plain
// "nothing here", in the caller's wording.
function emptyStateMarkup(view) {
    const options = view || {};
    const labels = options.labels || {};

    if (options.searchActive && options.searching) {
        return emptyBlock(labels.search_loading);
    }
    if (options.loadingNote) {
        return emptyBlock(options.loadingNote);
    }
    if (options.searchActive) {
        return searchEmptyMarkup(options);
    }
    return emptyBlock(options.quietMessage, historyWindowHint(options));
}

// Nothing matched. Below the message, a hint names whatever silently limited the
// scan, and - when there is anything left to reach - one button widens it by a
// single step. With everything already covered neither appears: the plain
// sentence is then the complete and honest answer.
function searchEmptyMarkup(view) {
    const labels = view.labels || {};
    const step = searchWideningStep(view);
    const hint = wideningHint(step, view);
    const action = wideningLabel(step, view);

    return '<div class="empty">' + esc(labels.search_no_match || labels.empty_filter || '')
        + (hint ? '<span class="empty-hint">' + esc(hint) + '</span>' : '')
        + (action ? '<button type="button" class="empty-action"' + attr('data-widen', step) + '>' + esc(action) + '</button>' : '')
        + '</div>';
}

// What limited the scan, phrased as the reason the button exists. Takes the whole
// view, like its sibling wideningLabel, so the two read alike at the call site.
function wideningHint(step, view) {
    const labels = view.labels || {};
    if (step === 'hidden') {
        return labels.search_scope_hint || '';
    }
    if (step === 'range') {
        return labels.history_range_hint || '';
    }
    return '';
}

// The offer itself. The range step names the window it would move to, so the
// click's cost is on the button rather than behind it.
function wideningLabel(step, view) {
    const labels = view.labels || {};
    if (step === 'hidden') {
        return labels.search_widen_hidden || '';
    }
    if (step !== 'range') {
        return '';
    }
    const next = nextHistoryRange(view.historyRange);
    if (!next) {
        return '';
    }
    const rangeLabel = labels[historyRange(next).label] || next;
    return (labels.search_widen_range || '').replace('{range}', rangeLabel);
}

// A bounded history window silently limits what can be found: only the past
// sessions inside it are listed, and only those are handed to the content
// search. So when nothing is left to show, name the window - otherwise an empty
// result reads as "that session does not exist" when it is merely out of view.
function historyWindowHint(view) {
    const options = view || {};
    if (!asKeySet(options.activeKeys).has('history') || historyRangeSeconds(options.historyRange) == null) {
        return '';
    }
    return (options.labels || {}).history_range_hint || '';
}

// How many rows are on screen against their chip, and the way back out of the
// widening. The count is plural-invariant by construction - the label carries no
// counted noun and the number comes last - like every other counted label here.
function widenedNoticeMarkup(count, labels) {
    const strings = labels || {};
    const tip = strings.search_widened_reset_tip;

    return '<div class="search-notice">'
        + '<span>' + esc((strings.search_widened_note || '').replace('{count}', count)) + '</span>'
        + '<button type="button" class="notice-reset"' + (tip ? attr('data-tip', tip) : '') + '>'
        + esc(strings.search_widened_reset || '') + '</button>'
        + '</div>';
}

// The filter chips active on a first launch (or any fallback): every chip
// except the ones that opt out (History), so the potentially large history scan
// only runs once the user asks for it. Deriving this - rather than "all chips" -
// is what keeps a fallback from silently enabling an off-by-default chip.
function defaultFilterKeys(filterDefs) {
    const keys = [];
    for (const def of filterDefs || []) {
        if (def && def.key && !def.offByDefault) {
            keys.push(def.key);
        }
    }
    return keys;
}

// The text the collapsed filter dropdown shows: the active filters' names in
// definition order, comma-separated. All on-by-default filters active at once
// read as one word (`filter_all`), since eight names spelled out say less than
// "all"; an opt-in filter (History) is still named beside it, with its time
// window, because what it adds depends on how far back it reaches. Nothing
// active reads `filter_none`.
function filterSummary(filterDefs, activeKeys, labels, historyRangeLabel) {
    const strings = labels || {};
    const active = asKeySet(activeKeys);
    const defs = (filterDefs || []).filter((def) => def && def.key);
    const defaults = defs.filter((def) => !def.offByDefault);
    const allDefaults = defaults.length > 0 && defaults.every((def) => active.has(def.key));

    const parts = allDefaults ? [strings.filter_all || ''] : [];
    for (const def of defs) {
        if (!active.has(def.key) || (allDefaults && !def.offByDefault)) {
            continue;
        }
        const name = strings[def.label] || def.key;
        parts.push(def.key === 'history' && historyRangeLabel ? name + ' (' + historyRangeLabel + ')' : name);
    }
    return parts.length > 0 ? parts.join(', ') : (strings.filter_none || '');
}

// What ascending and descending mean for each sort field, in the field's own
// terms - "newest first" says what a list will look like, an arrow does not.
// Each pair is [asc, desc]: an ascending activity sort orders by age, so the
// newest comes first; an ascending model sort follows MODEL_RANK, smallest
// model first; an ascending status sort follows STATUS_ORDER, most urgent
// first.
const SORT_DIRECTION_LABELS = new Map([
    ['activity', ['sort_dir_newest', 'sort_dir_oldest']],
    ['usage', ['sort_dir_lowest', 'sort_dir_highest']],
    ['model', ['sort_dir_smallest', 'sort_dir_largest']],
    ['host', ['sort_dir_a_z', 'sort_dir_z_a']],
    ['status', ['sort_dir_urgent', 'sort_dir_calm']],
]);

// The two directions for a sort field as [{dir, label}], asc first. An unknown
// field gets the activity wording, matching the activity sort it falls back to.
function sortDirectionOptions(sortKey, labels) {
    const strings = labels || {};
    const [ascKey, descKey] = SORT_DIRECTION_LABELS.get(sortKey) || SORT_DIRECTION_LABELS.get('activity');
    return [
        { dir: 'asc', label: strings[ascKey] || ascKey },
        { dir: 'desc', label: strings[descKey] || descKey },
    ];
}

// The text the collapsed sort dropdown shows, comma-separated like the filter
// dropdown's: priority ordering when it is on (it is the primary key), then the
// field, then the direction in that field's own words.
function sortSummary(sortKey, fieldLabel, sortDir, priorityOrder, labels) {
    const strings = labels || {};
    const direction = sortDirectionOptions(sortKey, strings).find((option) => option.dir === sortDir)
        || sortDirectionOptions(sortKey, strings)[0];
    const parts = priorityOrder ? [strings.priority_order || ''] : [];
    parts.push(fieldLabel, direction.label);
    return parts.filter(Boolean).join(', ');
}

// Whether a session passes the content-search filter. A null match set means no
// search has produced results yet - a fresh query still inside its typing
// debounce, or a cleared/invalid one - so nothing is filtered and every session
// shows. An empty Map means a search actually ran and matched nothing, so it
// correctly hides everything. Without the null case a just-typed query would
// briefly hide every row and flash a false "nothing matches".
function sessionMatchesSearch(sessionId, searchActive, searchMatches) {
    if (!searchActive || searchMatches == null) {
        return true;
    }
    return searchMatches.has(sessionId);
}

/* --- search hits --- */

// What each excerpt's `kind` token is called. A Map, not an object, because the
// token is backend data: an object lookup would resolve `constructor` to an
// inherited value and render a function into the row. An unknown kind names no
// source at all - the excerpt still reads fine without one.
const SEARCH_HIT_LABEL_KEYS = new Map([
    ['user', 'search_hit_user'],
    ['assistant', 'search_hit_assistant'],
    ['thinking', 'search_hit_thinking'],
    ['tool_use', 'search_hit_tool'],
    ['tool_result', 'search_hit_tool_result'],
    ['system', 'search_hit_system'],
    ['summary', 'search_hit_summary'],
]);

// The found passages beneath a matched row: how many hits the transcript holds,
// then the excerpts the backend sent for the first few of them. Only those
// excerpts exist here - the rest of the transcript never reached the interface -
// so "not shown" is a real remainder, not a collapsed section.
//
// Both counts are plural-invariant by construction: the label carries no counted
// noun and the number comes last, like every other counted label here. A count
// the backend had to stop short of is suffixed "+", which needs no wording of
// its own in any language.
function searchHitsMarkup(hit, labels) {
    const count = hit ? Number(hit.count) : 0;
    if (!Number.isFinite(count) || count <= 0) {
        return '';
    }

    const strings = labels || {};
    const snippets = Array.isArray(hit.snippets) ? hit.snippets : [];
    const partial = hit.partial ? '+' : '';

    let html = '<div class="row-hits">'
        + '<span class="hit-count">' + esc(fmt(strings.search_hits_count, { count: count + partial })) + '</span>';

    for (const snippet of snippets) {
        html += searchHitMarkup(snippet, strings);
    }

    const remaining = count - snippets.length;
    if (remaining > 0) {
        html += '<span class="hit-more">' + esc(fmt(strings.search_hits_more, { count: remaining + partial })) + '</span>';
    }

    return html + '</div>';
}

// One excerpt: where it came from, then the text with the hit marked. The three
// text pieces are escaped separately so the <mark> around the hit is the only
// markup in the line - nothing from a transcript can contribute a tag.
function searchHitMarkup(snippet, labels) {
    if (!snippet || typeof snippet !== 'object') {
        return '';
    }

    const source = searchHitSource(snippet, labels);
    const before = (snippet.clipped_before ? '…' : '') + String(snippet.before == null ? '' : snippet.before);
    const after = String(snippet.after == null ? '' : snippet.after) + (snippet.clipped_after ? '…' : '');

    return '<span class="hit">'
        + (source ? '<span class="hit-source">' + esc(source) + '</span>' : '')
        + '<span class="hit-text">' + esc(before) + '<mark>' + esc(snippet.match) + '</mark>' + esc(after) + '</span>'
        + '</span>';
}

// A tool call names the tool itself - "Bash" says far more than "Tool" - and
// everything else takes its kind's label. The name is on-disk data, so it is
// passed on as text and escaped by the caller, never looked up as a key.
function searchHitSource(snippet, labels) {
    if (typeof snippet.tool === 'string' && snippet.tool) {
        return snippet.tool;
    }

    const key = SEARCH_HIT_LABEL_KEYS.get(snippet.kind);
    return key ? (labels[key] || '') : '';
}

// A fire-and-forget bridge call returns a promise, so a Python-side rejection
// escapes a plain try/catch and lands in the global unhandledrejection handler -
// which wipes the whole content area. Invoke the call, catch a synchronous
// throw, and attach a rejection handler so an async failure is contained too;
// onError runs for either. Returns the settled promise (for tests / optional
// chaining), or undefined on a synchronous throw.
function settleCall(thunk, onError) {
    const handle = (error) => {
        if (typeof onError === 'function') {
            try {
                onError(error);
            } catch (inner) {
                // Cleanup must never re-throw and re-enter the global handler.
            }
        }
    };

    let result;
    try {
        result = thunk();
    } catch (error) {
        handle(error);
        return undefined;
    }

    if (result && typeof result.then === 'function') {
        return result.then(undefined, handle);
    }
    return result;
}

// Full status for one raw record, combining the transcript-derived state with
// the registry's busy/idle field and any background work.
function deriveStatus(raw) {
    let status = classify({
        alive: raw.alive,
        has_transcript: raw.has_transcript,
        last_stop_reason: raw.last_stop_reason,
        pending_tool: raw.pending_tool,
        pending_blocking: pendingBlockReason(raw) !== null,
        has_activity: raw.has_activity,
        last_entry_kind: raw.last_entry_kind,
        turn_predates_process: turnPredatesProcess(raw),
        turn_end_blocked: raw.turn_end_signal === 'blocked',
    });

    if (raw.alive) {
        status = refineWithNative(status, raw.native_status, raw.waiting_for);
        status = refineWithBackgroundWork(status, backgroundWorkRunning(raw));
        status = refineWithWakeup(status, wakeupPending(raw));
    }
    return status;
}

// Whether work the session started is still running beside a finished turn. A
// stopped turn (see turnStopped) tore down its in-process subagents and
// workflows, so a still-"running" count or a still-"active" workflow is a
// phantom until the recent window clears it - it must not promote the session to
// "processing". A detached OS child process (a build or server) can outlive the
// stop, so it counts.
function backgroundWorkRunning(raw) {
    const stopped = turnStopped(raw);
    const subagentsRunning = stopped ? 0 : (raw.subagents_running || 0);
    const workflowRunning = !stopped && activeWorkflows(raw.workflows).length > 0;

    return subagentsRunning > 0 || childRunning(raw) || workflowRunning;
}

// Whether a self-paced /loop has scheduled its next run and it is still ahead.
// The wakeup lives in the memory of the process that scheduled it, so a session
// reopened since has none; and a turn that ended on a question or a block is
// waiting for you whatever the loop planned. The deadline is the one Claude Code
// itself wrote (the call's time plus its delay), not elapsed silence, so a
// thinking session is never touched: its turn has not ended.
function wakeupPending(raw) {
    const due = raw.wakeup_in_seconds == null ? NaN : Number(raw.wakeup_in_seconds);

    return Boolean(raw.alive) && !turnPredatesProcess(raw) && !raw.turn_end_signal && Number.isFinite(due) && due > 0;
}

// A finished turn whose loop will wake the session on its own is not your turn:
// the agent goes on without you, which is what "processing" says. Only the calm
// finished state is promoted - an interrupt means you stopped the loop.
function refineWithWakeup(status, pending) {
    return pending && status === 'awaiting_input' ? 'processing' : status;
}

/* --- pending-tool blocking + mode (ported from formatting.py) --- */

const QUESTION_TOOLS = new Set(['AskUserQuestion']);
const PLAN_TOOLS = new Set(['ExitPlanMode', 'EnterPlanMode']);
const DIALOG_TOOLS = new Set(['AskUserQuestion', 'ExitPlanMode', 'EnterPlanMode']);

// The modes in which a pending generic tool can be a permission prompt rather
// than a tool executing. `acceptEdits` belongs here because it is auto-EDIT, not
// auto-everything: it waives the prompt for the file-editing tools below and
// for nothing else, so a command or a fetch still asks. Reading it as a blanket
// allow left a session sitting on an unanswered permission prompt reading
// "working" - and permanently, since nothing is appended while the prompt waits
// (only the stalled fallback eventually caught it, five minutes late).
// `dontAsk` is deliberately absent for the opposite reason: it auto-denies
// whatever would have prompted, so that session never waits for you at all and
// a call pending there is one that is executing.
const PROMPTING_MODES = new Set(['default', 'acceptEdits']);

// The tools whose prompt `acceptEdits` waives - the ones that change a file.
const AUTO_EDIT_TOOLS = new Set(['Edit', 'MultiEdit', 'Write', 'NotebookEdit']);

// How long a pending tool_use may sit with no child process and no transcript
// growth before it stops counting as a tool that is executing. This is the one
// place elapsed time enters the status model, and it is deliberately narrow: it
// applies only while a tool_use is unanswered, never to the thinking case the
// structural rule exists for (a think has no open tool_use, so it is untouched).
// Past this point the two remaining readings - an open permission dialog, or a
// session abandoned mid-call - both put the ball in the user's court, so the row
// stops claiming the agent is working. Wide enough to clear any ordinary
// in-process call; the residual cost is a call that legitimately runs longer
// with no child process to show for it (a slow MCP tool, whose server started
// with the session and is therefore excluded from child_count), which reads as
// blocked - traded deliberately for never stranding an abandoned session on
// "working", where nothing is ever appended to clear it.
const STALLED_PENDING_SECONDS = 300;

// The same window in `auto` mode, where the standstill means something else.
// Auto mode replaces your approval with a classifier that answers in seconds,
// so a call still unanswered long after that is no longer plausibly under
// review - and auto mode does prompt: an explicit ask rule, an MCP tool that
// requires interaction, and the pause Claude Code falls back to after repeated
// classifier blocks all put a dialog on the screen that no mode table predicts.
// None of it is marked on disk - measured over an open dialog, the registry
// file is not rewritten and the transcript grows by nothing - so this shorter
// window is all that stands between such a session and five minutes of
// claiming to work. It buys that at the known cost of the long window, only
// sooner: an in-process call with no child process that legitimately outlives
// it reads as blocked.
const AUTO_STALLED_PENDING_SECONDS = 90;

// A `Map`, not an object, for the same reason the API-error maps are: the mode
// is untrusted on-disk data, and an object lookup would answer `constructor` or
// `toString` with an inherited function - which then reaches the mode chip.
const MODE_LABELS = new Map([
    ['default', 'Manual'],
    ['acceptEdits', 'Auto-edit'],
    ['auto', 'Auto'],
    ['plan', 'Plan'],
    ['dontAsk', "Don't ask"],
    ['bypassPermissions', 'Bypass'],
]);

// How many classifier blocks in a row put auto mode on hold. Claude Code's own
// threshold, and the reason a session in `auto` can start asking about
// everything: past this run it stops deferring to the classifier and prompts
// you again, until you approve one. Python ships the raw count; reading a
// paused mode out of it is a classification, so it happens here.
const AUTO_PAUSE_STREAK = 3;

// Whether auto mode has put itself on hold on this session. Live sessions only:
// the hold belongs to a running CLI, and the streak an ended session stopped on
// describes nothing that is still true - nor does one an earlier process ran up
// before the session was reopened: the counter lives in that process's memory,
// and a new process starts at zero.
function autoModePaused(raw) {
    const streak = Number(raw.auto_denials_consecutive);

    return Boolean(raw.alive) && !turnPredatesProcess(raw) && Number.isFinite(streak) && streak >= AUTO_PAUSE_STREAK;
}

function pendingIsBlocking(toolName, permissionMode) {
    if (DIALOG_TOOLS.has(toolName)) {
        return true;
    }
    if (!PROMPTING_MODES.has(permissionMode)) {
        return false;
    }
    // The one mode that prompts for some tools and not others.
    if (permissionMode === 'acceptEdits') {
        return !AUTO_EDIT_TOOLS.has(toolName);
    }
    return true;
}

// Whether the session has a meaningful descendant process, i.e. a tool that
// spawned one is executing right now. Read by both the pending-tool reasoning
// and the background-work promotion, so the two can never drift apart.
function childRunning(raw) {
    return (raw.child_count || 0) > 0;
}

// Whether work is running inside the session's own process - a subagent, or a
// workflow bridging the pause between two fan-out phases. Neither spawns an OS
// child, so the child gate cannot speak for them, yet the call they hang off (a
// pending Task) is executing exactly like a build with a child is, and it is a
// call no permission mode ever prompts for. No force-stop guard is needed here,
// unlike in deriveStatus / buildSession where a phantom count must not promote or
// badge a stopped session: classify settles an interrupt or an API error before it
// ever consults the pending-tool reason, and the reason's tool name is read only
// for a session that came out of it as awaiting_permission.
function inProcessWorkRunning(raw) {
    return (raw.subagents_running || 0) > 0 || activeWorkflows(raw.workflows).length > 0;
}

// Why a session's pending tool_use blocks the user, or null when it does not
// block at all. The reason is what the caller needs, not just the boolean: only
// a named dialog or a real permission prompt may be spelled out in the label -
// a stalled call cannot say what it is waiting for, so it keeps the neutral one.
function pendingBlockReason(raw) {
    if (!raw.pending_tool) {
        return null;
    }
    // A dialog tool (question / plan review) never spawns a child process, so a
    // live child alongside a pending dialog is unrelated background work and must
    // not demote the block - the dialog is waiting on you in every mode.
    if (DIALOG_TOOLS.has(raw.last_tool_name)) {
        return 'dialog';
    }
    // A running child means a generic tool is executing, never prompting - and so
    // does a subagent or workflow working under a pending Task call, which shows
    // no child to prove it.
    if (childRunning(raw) || inProcessWorkRunning(raw)) {
        return null;
    }
    if (pendingIsBlocking(raw.last_tool_name, raw.permission_mode)) {
        return 'prompt';
    }
    return pendingIsStalled(raw.age_seconds, raw.permission_mode) ? 'stalled' : null;
}

// A pending tool_use whose transcript has stood still this long is not running.
// The finite check is load-bearing in one direction: a missing or mistyped age
// yields NaN, which fails the comparison on its own, but Infinity would pass it
// and declare a session with no usable age stalled. An unusable age is no
// evidence at all, so it degrades to the calmer reading - the session keeps
// whatever the structural rule said - rather than raising attention on a
// number the snapshot could not supply.
function pendingIsStalled(ageSeconds, permissionMode) {
    const age = Number(ageSeconds);

    return Number.isFinite(age) && age >= stalledPendingWindow(permissionMode);
}

// How long a pending call may stand still before it stops counting as running.
// Only `auto` shortens it; every other mode keeps the long window, `dontAsk`
// included - a would-be prompt is denied there rather than shown, so its
// standstill is the ordinary kind.
function stalledPendingWindow(permissionMode) {
    return permissionMode === 'auto' ? AUTO_STALLED_PENDING_SECONDS : STALLED_PENDING_SECONDS;
}

// The slack turnPredatesProcess allows between a turn's timestamp and the
// registry's process start. Both are written by the same process within a second
// of each other when a session starts (the registry first, as observed, though
// the slack does not depend on that order), whereas a real reopen puts the old
// turn seconds to days behind the new process. Not a freshness window: the two
// ages grow at the same rate, so their difference is fixed for a given turn and
// process, and the reading changes only when a new turn is appended.
const PROCESS_START_SLACK_SECONDS = 5;

// Whether the newest turn was written before the process now holding the session
// started - the session was reopened after an earlier process ended, and this one
// has not taken a turn yet. Structural, not time-based: see the slack above. The
// display age is exact enough for the comparison even though a running subagent
// can freshen it: a genuine subagent presupposes a Task call this process wrote
// (a younger turn), and a phantom one from the earlier process is itself older
// than this process. A missing or unusable age on either side is no evidence and
// reads as not reopened - null is checked explicitly, since Number(null) is 0 and
// would read a missing process age as a process that started just now. So does a
// transcript with no turn at all, which has nothing to predate.
function turnPredatesProcess(raw) {
    if (!raw.has_activity) {
        return false;
    }
    const age = raw.age_seconds == null ? NaN : Number(raw.age_seconds);
    const processAge = raw.process_age_seconds == null ? NaN : Number(raw.process_age_seconds);

    return Number.isFinite(age) && Number.isFinite(processAge) && age - processAge > PROCESS_START_SLACK_SECONDS;
}

// Whether the newest turn is over with nothing it started in-process surviving:
// a force stop (an interrupt, or an API error such as a usage limit) tears down
// subagents and workflows, and so does the end of a reopened session's earlier
// process. Read where a still-"running" count or "active" workflow must neither
// promote nor badge the session; a detached OS child process can outlive all
// three, so child_count is not gated on it.
function turnStopped(raw) {
    return raw.last_entry_kind === 'user_interrupt' || raw.last_entry_kind === 'api_error' || turnPredatesProcess(raw);
}

function modeLabel(permissionMode) {
    if (!permissionMode) {
        return null;
    }
    return MODE_LABELS.get(permissionMode) || permissionMode;
}

/* --- label formatting (ported from formatting.py) --- */

/* An API-error turn carries two raw fields - Claude Code's own error token and
   the HTTP status of the failed call - and naming the cause from them is a
   classification, so it happens here rather than in Python. Neither field is
   ever guaranteed: a lost connection has no status, and the token set is an
   unversioned internal, so an unrecognized pair falls back to the bare status
   and finally to the plain "Error" label. A raw token is never shown - it would
   put an untranslated internal name in a localized UI.

   The status is consulted first because it is the more specific of the two: an
   overload arrives as a plain `server_error` with status 529. */
const API_ERROR_STATUS_LABELS = new Map([
    [429, 'status_usage_limit'],
    [529, 'error_overloaded'],
]);

// Both spellings of each cause: the token Claude Code writes into the entry, and
// the API error type it wraps - either can show up, and they mean the same.
const API_ERROR_KIND_LABELS = new Map([
    ['rate_limit', 'status_usage_limit'],
    ['rate_limit_error', 'status_usage_limit'],
    ['overloaded_error', 'error_overloaded'],
    ['server_error', 'error_server'],
    ['internal_server_error', 'error_server'],
    ['api_error', 'error_server'],
    ['invalid_request', 'error_invalid_request'],
    ['invalid_request_error', 'error_invalid_request'],
    ['request_too_large', 'error_invalid_request'],
    ['authentication_failed', 'error_auth'],
    ['authentication_error', 'error_auth'],
    ['permission_error', 'error_auth'],
]);

function statusLabel(status, labels) {
    return labels['status_' + status] || status;
}

// The HTTP status as a positive integer, or null when the entry carries none or
// a value that does not read as one.
function apiErrorStatusCode(raw) {
    const status = Number(raw && raw.api_error_status);

    return Number.isFinite(status) && status > 0 ? Math.trunc(status) : null;
}

// The label for a session whose last turn stopped on an API error: the cause
// named where the error's own fields identify one ("Error: servers overloaded"),
// the bare status where only that is known ("Error: HTTP 521"), and null when
// there is nothing to add to the plain "Error" - which is what the caller then
// falls back to. A usage limit keeps its own standalone label: it is the one
// cause that is not a fault to report but a wait to sit out.
function apiErrorLabel(raw, labels) {
    const status = apiErrorStatusCode(raw);
    const kind = typeof (raw && raw.api_error_kind) === 'string' ? raw.api_error_kind.trim().toLowerCase() : '';
    const reasonKey = (status !== null ? API_ERROR_STATUS_LABELS.get(status) : null)
        || API_ERROR_KIND_LABELS.get(kind)
        // Any other 5xx is a server-side failure whatever the token says.
        || (status !== null && status >= 500 ? 'error_server' : null);

    if (reasonKey === 'status_usage_limit') {
        return usageLimitLabel(raw, labels);
    }

    const reason = reasonKey ? labels[reasonKey] : (status !== null ? fmt(labels.error_http, { status: status }) : '');
    if (!reason) {
        return null;
    }

    return fmt(labels.status_errored_reason, { reason: reason }) || null;
}

// Which usage limit the error entry's quotaLimits names. A `Map` because the
// type is untrusted on-disk data; a limit it does not know reads as the generic
// one, and the per-model weekly limits are weekly limits all the same.
const RATE_LIMIT_LABEL_KEYS = new Map([
    ['five_hour', 'limit_five_hour'],
    ['seven_day', 'limit_seven_day'],
    ['seven_day_opus', 'limit_seven_day'],
    ['seven_day_sonnet', 'limit_seven_day'],
]);

// A usage limit that says when it resets is worth more than "Usage limit
// reached": the reset is when you can go on, and once it has passed the session
// is no longer waiting for anything but you. The reset is the time Claude Code
// itself wrote into the error entry, counted down against the clock - a
// deadline, not elapsed silence. Without a readable reset the plain label stays.
function usageLimitLabel(raw, labels) {
    const resetsIn = raw.rate_limit_resets_in_seconds == null ? NaN : Number(raw.rate_limit_resets_in_seconds);
    const limit = labels[RATE_LIMIT_LABEL_KEYS.get(raw.rate_limit_type) || 'limit_usage'];
    if (!Number.isFinite(resetsIn) || !limit) {
        return labels.status_usage_limit || null;
    }

    const label = resetsIn > 0
        ? fmt(labels.status_limit_resets, { limit: limit, time: formatAge(resetsIn, labels) })
        : fmt(labels.status_limit_over, { limit: limit });

    return label || labels.status_usage_limit || null;
}

// The dot's hover text: the translated status label, with the detail lines that
// say more under it - the error entry's own line, how long the session has been
// in its status, what started the turn. One string joined by newlines, which the
// tooltip renders as line breaks (white-space: pre-line); the row and the panel
// header keep the label alone, both being single-line. A single detail may be
// passed bare.
function statusTip(label, detail) {
    const details = Array.isArray(detail) ? detail : [detail];
    const lines = [];
    for (const item of details) {
        const text = typeof item === 'string' ? item.trim() : '';
        if (text) {
            lines.push(text);
        }
    }
    if (!lines.length) {
        return label;
    }

    return label ? [label].concat(lines).join('\n') : lines.join('\n');
}

function attentionLabel(status, pendingToolName, labels, errorLabel) {
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
    // A stuck-on-error session names its cause where the error entry's own
    // fields identify one - the usage/session limit that only needs waiting out,
    // an overload, a rejected request - because "Error" alone says nothing about
    // what to do next. An unrecognized error keeps the plain label.
    if (status === 'errored') {
        return errorLabel || statusLabel(status, labels);
    }
    return statusLabel(status, labels);
}

// A more specific label than the status's own, where the transcript or the
// registry says more about the state than the status can - or null to keep the
// plain one. Each refinement speaks only for the status it describes:
//   - working: a request the CLI is retrying, offline or not;
//   - processing: a loop asleep until its next run, when nothing else runs -
//     beside a subagent or a build, "Background" is the truer word;
//   - awaiting_permission with no tool to name: a wrap-up call that ended the
//     turn blocked, or the registry's "input needed", which current Claude Code
//     writes for an open question only (a "permission prompt" stays neutral -
//     earlier versions wrote it for questions too);
//   - awaiting_input: a turn that ended on a question for you.
function refinedStatusLabel(status, raw, labels) {
    if (status === 'working') {
        return retryLabel(raw, labels);
    }
    if (status === 'processing' && !backgroundWorkRunning(raw) && wakeupPending(raw)) {
        return fmt(labels.status_sleeping, { time: formatAge(Number(raw.wakeup_in_seconds), labels) }) || null;
    }
    if (status === 'awaiting_permission' && !namedPendingTool(raw)) {
        if (raw.turn_end_signal === 'blocked') {
            return labels.status_blocked || null;
        }
        if (raw.native_status === 'waiting' && raw.waiting_for === 'input needed') {
            return labels.status_question || null;
        }
    }
    if (status === 'awaiting_input' && raw.turn_end_signal === 'ask') {
        return labels.status_asks || null;
    }
    return null;
}

// "Retrying (2/10)" for a request the CLI is retrying, or its offline variant
// when Claude Code itself found the network down. Both counters must read as
// positive integers, or there is nothing to say beyond "Working".
function retryLabel(raw, labels) {
    const attempt = Number(raw.api_retry_attempt);
    const max = Number(raw.api_retry_max);
    if (!Number.isInteger(attempt) || !Number.isInteger(max) || attempt <= 0 || max <= 0) {
        return null;
    }

    return fmt(raw.api_retry_offline ? labels.status_offline : labels.status_retrying, { attempt: attempt, max: max }) || null;
}

// Which derived status each native registry status confirms. How long the
// registry has held its status is shown only where the two agree - a lagging
// "idle" beside a session the transcript shows working says nothing about how
// long it has worked.
const NATIVE_STATUS_MATCH = new Map([
    ['busy', 'working'],
    ['idle', 'awaiting_input'],
    ['waiting', 'awaiting_permission'],
]);

// What opened the running turn, where that is worth a line: a background task
// that finished, or a message from another session. A turn you started yourself
// needs no explanation.
const TURN_ORIGIN_LABEL_KEYS = new Map([
    ['task-notification', 'origin_task'],
    ['peer', 'origin_peer'],
]);

// The tooltip lines under the status label (see statusTip), in reading order:
// the CLI's own wording of an error or of a failed request it is retrying, what
// started the running turn, and how long the session has been in its status.
function statusDetailLines(status, raw, labels) {
    const lines = [];
    if (status === 'errored') {
        lines.push(raw.api_error_detail);
    }
    if (status === 'working') {
        if (retryLabel(raw, labels)) {
            lines.push(raw.api_retry_detail);
        }
        const originKey = TURN_ORIGIN_LABEL_KEYS.get(raw.turn_origin);
        if (originKey) {
            lines.push(labels[originKey]);
        }
    }
    const since = raw.native_status_age_seconds == null ? NaN : Number(raw.native_status_age_seconds);
    if (NATIVE_STATUS_MATCH.get(raw.native_status) === status && Number.isFinite(since)) {
        lines.push(fmt(labels.status_for, { time: formatAge(since, labels) }));
    }
    return lines;
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

// Format an age whose base value was captured at a past moment, advancing it to
// now. A history row captures its age once, at fetch time, so it must age from
// THAT epoch rather than the latest live-snapshot poll - otherwise its age
// freezes near the fetch-time value while the true age grows. capturedAtMs and
// nowMs are epoch milliseconds; a non-finite capturedAtMs formats the base as-is.
function formatAgeSince(baseSeconds, capturedAtMs, nowMs, labels) {
    if (baseSeconds == null) {
        return '';
    }
    if (!Number.isFinite(capturedAtMs) || !Number.isFinite(nowMs)) {
        return formatAge(baseSeconds, labels);
    }
    const elapsed = Math.max(0, (nowMs - capturedAtMs) / 1000);
    return formatAge(baseSeconds + elapsed, labels);
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
        // Decide the tier and precision after rounding, so a value that rounds up
        // across a boundary is promoted cleanly: 999,500 -> "1.0M" (not "1000k"),
        // and 99,955 -> "100k" (not "100.0k", matching 100,000).
        if (Math.round(thousands) >= 1000) {
            return (value / 1000000).toFixed(1) + 'M';
        }
        const oneDecimal = Math.round(thousands * 10) / 10;
        return oneDecimal >= 100 ? Math.round(thousands) + 'k' : oneDecimal.toFixed(1) + 'k';
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
        // Own-property check: a key like "constructor"/"toString" would otherwise
        // resolve to an inherited Object.prototype member (truthy) and price the
        // model at $0 instead of falling back to the token total. modelId comes
        // from untrusted on-disk transcripts.
        const rate = key && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;
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

/* --- cost parts (main conversation, subagents, skills, web searches) ---

   The backend ships a session's usage per model for the whole session - its
   own transcript and every subagent's - plus `usage_parts`: the same usage
   split into the main conversation, each kind of subagent and each skill a
   turn ran under, and the number of web searches. The parts only explain the
   total; they never change it. */

// How many entries a list of parts names before it counts the rest, and the
// mark each entry's line starts with.
const COST_PART_LIMIT = 4;
const COST_PART_BULLET = '• ';

function isRecord(value) {
    return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

// The price of one web search from the resolved table's `web_search` entry, or
// null when the table names none. Own-property check for the reason
// sessionCostUsd has one: the table is data, not code.
function webSearchPrice(prices) {
    const entry = isRecord(prices) && Object.prototype.hasOwnProperty.call(prices, 'web_search') ? prices.web_search : null;
    const price = isRecord(entry) ? Number(entry.per_request) : NaN;
    return Number.isFinite(price) && price >= 0 ? price : null;
}

function webSearchCount(raw) {
    const parts = raw ? raw.usage_parts : null;
    return isRecord(parts) ? (positiveInteger(parts.web_searches) || 0) : 0;
}

// The session's estimated cost: every model's tokens plus its web searches.
// Null when a model has no rate, exactly like sessionCostUsd.
function sessionTotalCostUsd(raw, prices) {
    const tokens = sessionCostUsd(raw ? raw.usage_by_model : null, prices);
    if (tokens === null) {
        return null;
    }
    const price = webSearchPrice(prices);
    return tokens + (price === null ? 0 : webSearchCount(raw) * price);
}

function modelUsageTokens(usageByModel) {
    let total = 0;
    for (const usage of Object.values(isRecord(usageByModel) ? usageByModel : {})) {
        total += usageTotalTokens(usage);
    }
    return total;
}

// A part's amount: its cost when every model in it has a rate, else its tokens.
function partAmount(usageByModel, prices) {
    const usd = sessionCostUsd(usageByModel, prices);
    return usd === null ? formatTokens(modelUsageTokens(usageByModel)) : formatCost(usd);
}

// One tooltip line per part, the most spend first ("• next:reviewer $30"); past
// COST_PART_LIMIT a last line counts the rest ("• +3"). The tooltip collapses
// leading spaces, so a bullet marks each line as belonging to the heading above.
function costPartLines(partsByName, prices, displayName) {
    const entries = [];
    for (const name of Object.keys(isRecord(partsByName) ? partsByName : {})) {
        const usageByModel = partsByName[name];
        const tokens = modelUsageTokens(usageByModel);
        if (tokens === 0) {
            continue;
        }
        entries.push({ name: displayName(name), usd: sessionCostUsd(usageByModel, prices), tokens: tokens });
    }
    entries.sort((first, second) => ((second.usd ?? -1) - (first.usd ?? -1)) || (second.tokens - first.tokens));

    const lines = entries.slice(0, COST_PART_LIMIT).map(
        (entry) => COST_PART_BULLET + entry.name + ' ' + (entry.usd === null ? formatTokens(entry.tokens) : formatCost(entry.usd)));
    if (entries.length > COST_PART_LIMIT) {
        lines.push(COST_PART_BULLET + '+' + (entries.length - COST_PART_LIMIT));
    }
    return lines;
}

// The sum of several per-model usages. Model ids and usage keys are on-disk
// data, so the objects have no prototype: a key such as `__proto__` stays an
// ordinary property instead of reaching Object.prototype.
function mergedModelUsage(usageByModelList) {
    const merged = Object.create(null);
    for (const usageByModel of usageByModelList) {
        for (const model of Object.keys(isRecord(usageByModel) ? usageByModel : {})) {
            if (!Object.prototype.hasOwnProperty.call(merged, model)) {
                merged[model] = Object.create(null);
            }
            const target = merged[model];
            for (const [key, value] of Object.entries(isRecord(usageByModel[model]) ? usageByModel[model] : {})) {
                target[key] = (target[key] || 0) + (Number(value) || 0);
            }
        }
    }
    return merged;
}

// The cost tooltip's lines: how the session's spend divides into the main
// conversation and its subagents (only when it ran any), into the skills its
// turns ran under, and what its web searches cost. Empty when there is nothing
// beyond the total to explain.
function costTipLines(raw, prices, labels) {
    const parts = raw ? raw.usage_parts : null;
    if (!isRecord(parts)) {
        return [];
    }
    const lines = [];

    const agents = isRecord(parts.agents) ? parts.agents : {};
    const agentUsage = mergedModelUsage(Object.keys(agents).map((name) => agents[name]));
    if (modelUsageTokens(agentUsage) > 0) {
        lines.push(fmt(labels.cost_main, { cost: partAmount(parts.main, prices) }));
        lines.push(fmt(labels.cost_agents, { cost: partAmount(agentUsage, prices) }));
        lines.push(...costPartLines(agents, prices, (name) => name || labels.cost_agent_unknown));
    }

    const skills = costPartLines(parts.skills, prices, (name) => name);
    if (skills.length > 0) {
        lines.push(labels.cost_skills, ...skills);
    }

    const searches = webSearchCount(raw);
    const price = webSearchPrice(prices);
    if (searches > 0 && price !== null) {
        lines.push(fmt(labels.cost_web_search, { count: searches, cost: formatCost(searches * price) }));
    }
    return lines;
}

/* --- cost per ticket ---

   A branch that names a ticket key (`feature/PNE-10617/...`) ties a session to
   that ticket; the sessions of one ticket - live, and past ones once the
   history has been loaded - are added up for the branch's tooltip. */

// A tracker key as Jira writes it: an upper-case project key, a hyphen and a
// number, standing on its own within the branch name.
const TICKET_KEY = /(?:^|[^A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,6})(?![A-Za-z0-9])/;

function ticketKey(branch) {
    if (typeof branch !== 'string') {
        return null;
    }
    const match = branch.match(TICKET_KEY);
    return match ? match[1] : null;
}

// Per ticket key: the summed cost (`priced` false once a session in it has no
// rate, when only the tokens are meaningful), the tokens and the session count.
// A session id seen twice counts once.
function ticketTotals(rawSessions, prices) {
    const totals = new Map();
    const seen = new Set();
    for (const raw of rawSessions || []) {
        if (!raw || seen.has(raw.session_id)) {
            continue;
        }
        seen.add(raw.session_id);
        const key = ticketKey(raw.git_branch);
        if (!key) {
            continue;
        }
        const total = totals.get(key) || { usd: 0, tokens: 0, sessions: 0, priced: true };
        const usd = sessionTotalCostUsd(raw, prices);
        if (usd === null) {
            total.priced = false;
        } else {
            total.usd += usd;
        }
        total.tokens += usageTotalTokens(raw.usage);
        total.sessions += 1;
        totals.set(key, total);
    }
    return totals;
}

// The branch tooltip's ticket lines, or '' when the branch names no ticket:
// what its sessions cost, and which sessions that covers - the live ones only
// until the history has been loaded.
function ticketTip(branch, totals, includesHistory, labels) {
    const key = ticketKey(branch);
    const total = key && totals instanceof Map ? totals.get(key) : null;
    if (!total) {
        return '';
    }
    const amount = total.priced ? formatCost(total.usd) : formatTokens(total.tokens);
    return fmt(labels.ticket_cost, { ticket: key, cost: amount, count: total.sessions })
        + '\n' + (includesHistory ? labels.ticket_scope_history : labels.ticket_scope_live);
}

/* --- host / entrypoint (ported from snapshot.py) --- */

// A `Map` for the reason MODE_LABELS is one: the entrypoint comes off disk, and
// an object lookup would resolve `constructor` to an inherited function.
const ENTRYPOINT_HOSTS = new Map([
    ['claude-vscode', 'VS Code'],
    ['claude-desktop', 'Claude Desktop'],
]);

function hostLabel(detected, entrypoint) {
    if (detected) {
        return detected;
    }
    if (entrypoint) {
        return ENTRYPOINT_HOSTS.get(entrypoint) || null;
    }
    return null;
}

// Names for the entrypoint token stamped on each transcript entry, for the
// version history: the host map plus the one token the host column never
// names, since a CLI session is named there by its detected terminal and marked
// by the CLI chip - a "CLI" fallback in hostLabel would double up. An unknown
// token is still a name of sorts and falls back to itself (a `Map` lookup, so an
// inherited property name cannot answer); no token names nothing.
const ENTRYPOINT_LABELS = new Map([...ENTRYPOINT_HOSTS, ['cli', 'CLI']]);

function entrypointLabel(entrypoint) {
    if (typeof entrypoint !== 'string' || !entrypoint) {
        return null;
    }
    return ENTRYPOINT_LABELS.get(entrypoint) || entrypoint;
}

// The record's origin: 'local' (the default - a session on this machine, or an
// older record with no origin field at all) or 'wsl:<distro>' for a session whose
// process runs inside that WSL distribution. Defaulting a non-string value
// here keeps every caller - buildSession, searchScopeRefs - origin-safe
// without its own null check.
function sessionOrigin(raw) {
    return typeof raw.origin === 'string' ? raw.origin : 'local';
}

function isWslOrigin(origin) {
    return origin.startsWith('wsl:');
}

// A WSL session's host names its distribution instead of the (Windows-only)
// detected/entrypoint host: the resolved origin_label (e.g. "Ubuntu") suffixed
// "(WSL)" so the origin reads at a glance, or the bare "WSL" when the backend
// could not resolve a label - never the redundant "WSL (WSL)".
function wslHostLabel(originLabel) {
    const label = originLabel || 'WSL';
    return label === 'WSL' ? 'WSL' : label + ' (WSL)';
}

// The registry's session kinds, mapped to their label keys. The tokens are
// Claude Code's, and they are not the words to show: a background session is
// written as `bg`, which is what leaked into the host column as a bare "bg"
// while the label for it sat unused. An unknown kind has no key - the caller
// falls back to the raw token, which is still better than nothing.
const KIND_LABEL_KEYS = new Map([
    ['interactive', 'kind_interactive'],
    ['bg', 'kind_background'],
]);

function kindLabelKey(kind) {
    return KIND_LABEL_KEYS.get(kind) || null;
}

// A session started with `claude --bg`: it runs under the background-agent
// daemon with no terminal and no window of its own, which is why it is told
// apart from every other session at all - neither the CLI marker nor the
// click-to-raise-its-window applies to it.
function isBackground(raw) {
    return raw.kind === 'bg';
}

function isViaCli(raw) {
    return Boolean(raw.via_cli) || raw.entrypoint === 'cli';
}

function isVscodeDeeplink(raw) {
    return raw.entrypoint === 'claude-vscode';
}

// Remote Control runs in the session's own process, so the connection ends with
// it - the registry record of an ended session still names the cloud session it
// was bridged to, and that must not read as reachable.
function isRemoteControlled(raw) {
    return raw.remote_control === true && Boolean(raw.alive);
}

/* --- project grouping (ported from snapshot.py) --- */

// Case-insensitive like Windows paths, and per origin: two distros can report
// the identical POSIX cwd, and those are genuinely different folders that
// must not share a panel - nor its open-folder target or collapse state.
function groupKey(cwd, origin) {
    const path = String(cwd).replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
    return (typeof origin === 'string' && origin ? origin : 'local') + '|' + path;
}

function displayCwd(cwd) {
    // Coerce like its siblings groupKey/projectName: a non-string cwd would
    // otherwise throw on .length/[1] and blank the whole snapshot render.
    cwd = String(cwd == null ? '' : cwd);
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

// Claude Desktop runs a chat without a project folder in a directory of its own,
// <data>\claude_<id>\scratch-workspaces\...\scratch-<date>-<hex>, one per chat, and
// names the place "This computer". Returns the scratch-workspaces folder for such
// a cwd, so those chats share one panel keyed and opened on it rather than one
// panel per generated folder name; null for every other path.
const SCRATCH_WORKSPACE_CWD = /^(.*[\\/]claude_[^\\/]+[\\/]scratch-workspaces)(?:[\\/][^\\/]+)*[\\/]scratch-\d{4}-\d{2}-\d{2}-[0-9a-z]+[\\/]*$/i;

function scratchWorkspaceRoot(cwd) {
    const match = SCRATCH_WORKSPACE_CWD.exec(String(cwd == null ? '' : cwd));
    return match ? match[1] : null;
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

// The Claude Code version timeline, same shape and ordering as modelHistory: one
// entry per contiguous run of a version written from one place, the last one
// being the version in use. A long session resumed after an upgrade spans more
// than one, which is what dates a mid-session change in the agent's behaviour.
// Each run also names its origin - where the process that wrote it ran (VS
// Code, the CLI) - because a version flipping back and forth every few seconds
// is two processes holding the same session at once, and only the origin beside
// each run makes that legible.
function cliHistory(timeline) {
    const entries = Array.isArray(timeline) ? timeline : [];
    const history = [];
    for (const entry of entries) {
        if (entry && typeof entry.version === 'string' && entry.version) {
            history.push({ time: entry.time, version: entry.version, origin: entrypointLabel(entry.entrypoint) });
        }
    }

    return history;
}

// Claude Code's public changelog. The only remote address in this application:
// nothing is ever requested from it - the URL is handed to a link the user has
// to click, which the host opens in the system browser (see PRIVACY.md).
const CHANGELOG_URL = 'https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md';

// A released version is three dot-separated numbers, which is also exactly what
// may reach the href below. Anything else (a dev build, a renamed field, a
// crafted value) fails this and is shown as plain text instead, so the URL can
// never carry anything but digits and dots.
const CLI_VERSION_PATTERN = /^\d+\.\d+\.\d+$/;

// Deep link to a version's changelog section, or null when the version is not a
// plain release number. GitHub's heading anchor for "## 2.1.224" drops the dots,
// giving "#21224".
function changelogUrl(version) {
    if (typeof version !== 'string' || !CLI_VERSION_PATTERN.test(version)) {
        return null;
    }

    return CHANGELOG_URL + '#' + version.split('.').join('');
}

// Whether the CLI-version column carries information for the sessions currently
// in view - the condition for showing it at all. True once the versions differ
// across sessions, or one session itself spans more than one run (an update, or
// a handover to a process elsewhere): with every session on the same single
// version the column would repeat one value per row, which is why it is left out
// entirely instead.
function cliColumnRelevant(sessions) {
    const list = Array.isArray(sessions) ? sessions : [];
    const seen = new Set();
    for (const session of list) {
        if (!session) {
            continue;
        }
        if (session.cli_switched) {
            return true;
        }
        if (session.cli_version) {
            seen.add(session.cli_version);
            if (seen.size > 1) {
                return true;
            }
        }
    }

    return false;
}

// The pending tool the status label may name, or null when it may not.
function namedPendingTool(raw) {
    const reason = pendingBlockReason(raw);

    return (reason === 'dialog' || reason === 'prompt') ? raw.last_tool_name : null;
}

// Turn one raw backend record into the display object the renderer consumes.
// Everything here is derived; age is kept numeric so the UI can tick it live.
function buildSession(raw, labels, prices) {
    const status = deriveStatus(raw);
    const toolRunning = childRunning(raw);
    const usage = raw.usage || {};
    const models = modelHistory(raw.model_timeline);
    const cliVersions = cliHistory(raw.cli_timeline);
    const origin = sessionOrigin(raw);
    const wsl = isWslOrigin(origin);
    const originLabel = (typeof raw.origin_label === 'string' && raw.origin_label) ? raw.origin_label : null;

    // In-process subagents and workflows die when the turn is force-stopped - by
    // an interrupt or an API error (a usage limit stops the whole CLI) - and with
    // the earlier process of a reopened session (see turnStopped), so any
    // still-"running" count or "active" workflow is a phantom the recent window
    // has yet to clear. Hide them here too, so the row does not show a running
    // subagent or workflow badge next to a stopped or idle status.
    const stopped = turnStopped(raw);
    const subagentsRunning = stopped ? 0 : (raw.subagents_running || 0);
    const subagentsLabels = stopped ? [] : (raw.subagents_labels || []);
    const workflows = stopped ? [] : activeWorkflows(raw.workflows);
    const workflowTotal = workflows.reduce((sum, workflow) => sum + (workflow.total || 0), 0);
    const workflowDone = workflows.reduce((sum, workflow) => sum + (workflow.done || 0), 0);

    // Two parts so the row can animate the reveal: a compact anchor shown by
    // default (the cost when it can be priced, else a plain token total so the
    // number is never wrong), and the per-category breakdown that slides open
    // before it on hover. Expanded reads "<breakdown> · <anchor>".
    const breakdown = tokenLabels(raw.usage, labels);
    const costText = formatCost(sessionTotalCostUsd(raw, prices)) || '';
    let usageCompact = '';
    let usageDetail = '';
    if (raw.scan_pending) {
        // The transcript's first full scan is still running in the background,
        // so the usage is unknown rather than zero - an ellipsis until it lands.
        usageCompact = '…';
    } else if (breakdown) {
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

    // Only name the tool when the block is one we can name. last_tool_name
    // lingers from a resolved tool, so the registry-`waiting` route (no pending
    // tool) must fall through to the neutral label - matching the deriveStatus
    // gate. A stalled call falls through for the opposite reason: a tool is
    // named, but nothing says it is waiting on a permission rather than simply
    // abandoned, so it must not claim one.
    // A refinement the transcript or registry supports (a retry, a sleeping
    // loop, a blocked wrap-up, an open question) outranks the generic wording.
    const statusText = refinedStatusLabel(status, raw, labels)
        || attentionLabel(status, namedPendingTool(raw), labels, apiErrorLabel(raw, labels));

    // That one label goes to the dot, the row and the panel header; the dot's
    // tooltip adds the lines only it has room for - the other two are
    // single-line. Each line is tied to the status it describes (see
    // statusDetailLines): the error's own wording only for `errored`, since it
    // describes a turn that stopped.
    const statusTipText = statusTip(statusText, statusDetailLines(status, raw, labels));

    // Details that describe the running process - its plan, the hooks it runs -
    // say nothing once it has ended.
    const alive = raw.alive === true;
    const context = contextUsage(raw);

    const session = {
        session_id: raw.session_id,
        pid: raw.pid,
        cwd: raw.cwd,
        is_history: Boolean(raw.is_history),
        name: raw.title || raw.short_name,
        title: raw.title || '',
        short_name: raw.short_name,
        kind: raw.kind,
        status: status,
        status_label: statusText,
        status_tip: statusTipText,
        needs_attention: needsAttention(status),
        model: formatModel(raw.model_id),
        model_switched: models.length > 1,
        model_history: models,
        // The version in use comes from the tail (cli_version), so a history
        // session - which is never fully scanned - still shows one, it just has
        // no upgrade history to go with it.
        cli_version: typeof raw.cli_version === 'string' ? raw.cli_version : '',
        cli_switched: cliVersions.length > 1,
        cli_history: cliVersions,
        usage_compact: usageCompact,
        usage_detail: usageDetail,
        usage_total: usageTotalTokens(usage),
        // Unknown while a scan is pending, like the figure it explains.
        cost_tip: raw.scan_pending ? '' : costTipLines(raw, prices, labels).join('\n'),
        subagents_running: subagentsRunning,
        subagents_done: raw.subagents_done || 0,
        subagents_labels: subagentsLabels,
        workflow_active: workflows.length > 0,
        workflow_total: workflowTotal,
        workflow_done: workflowDone,
        processes: raw.child_count || 0,
        tool_running: toolRunning,
        origin: origin,
        wsl: wsl,
        origin_label: originLabel,
        host: wsl ? wslHostLabel(originLabel) : hostLabel(raw.host, raw.entrypoint),
        // `claude --bg` starts the session from a terminal that then goes away,
        // so the entrypoint says `cli` while no terminal is attached. Marking it
        // "CLI" would promise a terminal window to raise; the kind says what it
        // really is.
        via_cli: !isBackground(raw) && isViaCli(raw),
        background: isBackground(raw),
        job_id: typeof raw.job_id === 'string' ? raw.job_id : '',
        remote_control: isRemoteControlled(raw),
        mode: modeLabel(raw.permission_mode),
        auto_paused: autoModePaused(raw),
        auto_denials_total: Number(raw.auto_denials_total) || 0,
        vscode_deeplink: isVscodeDeeplink(raw),
        worktree: worktreeOf(raw),
        branch: branchOf(raw),
        // Whether the row names the branch itself; groupProjects sets it when the
        // panel's sessions are on different branches.
        show_branch: false,
        effort: effortOf(raw),
        fast_mode: isFastMode(raw),
        context: context,
        context_warn: Boolean(context && context.ratio !== null && context.ratio >= NEAR_LIMIT_RATIO),
        plan: alive ? planProgress(raw) : null,
        queued: QUEUE_STATUSES.has(status) ? (positiveInteger(raw.queued) || 0) : 0,
        budget: budgetOf(raw),
        hook_errors: alive ? hookErrorsOf(raw) : [],
        pull_requests: pullRequestsOf(raw),
        files_edited: positiveInteger(raw.files_edited) || 0,
        compactions: positiveInteger(raw.compactions) || 0,
        artifacts: positiveInteger(raw.artifact_count) || 0,
        age_seconds: raw.age_seconds == null ? null : Math.floor(raw.age_seconds),
    };
    session.info_tip = sessionInfoTip(session, labels);

    return session;
}

// A session that switched into a git worktree is grouped under the project it
// was launched in - the backend files it there, beside its transcript - so the
// directory it actually works in has to be shown on the row itself. Null for
// every other session.
function worktreeOf(raw) {
    const path = typeof raw.worktree_cwd === 'string' ? raw.worktree_cwd : '';
    if (!path) {
        return null;
    }
    return {
        name: projectName(path),
        path: displayCwd(path),
        branch: typeof raw.worktree_branch === 'string' ? raw.worktree_branch : '',
    };
}

/* --- session details (branch, context, plan, queue, budget, hooks, PRs) ---

   Display-only: none of these feeds the status. Each takes the raw record and
   returns a plain value or null, so a missing, renamed or mistyped field shows
   nothing instead of a wrong figure. */

// The share of the context window from which the row warns that a compaction is
// near. Below it the figure stays in the title's tooltip.
const NEAR_LIMIT_RATIO = 0.8;

// Statuses during which a turn is running, the only time a prompt can sit in
// Claude Code's queue: once the turn ends, a queued prompt is delivered at once,
// so a count read while idle would only be a leftover the tail failed to pair.
const QUEUE_STATUSES = new Set(['working', 'processing', 'awaiting_permission']);

// An effort level or speed exactly as Claude Code writes it (`high`, `xhigh`,
// `fast`); anything else is not shown.
const LABEL_TOKEN = /^[a-z][a-z0-9_-]{0,15}$/;

// A pull request's web address may reach an href only in this shape: https, a
// host, and path segments of unreserved characters - no query, no fragment, no
// quote, no `user@` part. Anything else is listed without a link.
const PULL_REQUEST_URL = /^https:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?(?:\/[A-Za-z0-9._~%+-]+)*\/?$/;
const PULL_REQUEST_URL_MAX = 400;

function positiveInteger(value) {
    if (typeof value === 'boolean') {
        return null;
    }
    const number = Number(value);
    return Number.isInteger(number) && number > 0 ? number : null;
}

function nonNegativeAmount(value) {
    if (value === null || value === undefined || typeof value === 'boolean') {
        return null;
    }
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : null;
}

// The current git branch, or '' when the transcript names none.
function branchOf(raw) {
    return typeof raw.git_branch === 'string' ? raw.git_branch : '';
}

// The latest reply's effort level, or ''.
function effortOf(raw) {
    return typeof raw.effort === 'string' && LABEL_TOKEN.test(raw.effort) ? raw.effort : '';
}

function isFastMode(raw) {
    return raw.speed === 'fast';
}

// How full the context was on the latest reply: the tokens it held and, when the
// model catalog names the window, its size and the share of it used.
function contextUsage(raw) {
    const used = positiveInteger(raw.context_tokens);
    if (used === null) {
        return null;
    }
    const size = positiveInteger(raw.context_window);

    return { used: used, size: size, ratio: size === null ? null : Math.min(1, used / size) };
}

// Claude Desktop's progress list: the step in progress and the step count, or
// null when there is none or every step is done.
function planProgress(raw) {
    const step = positiveInteger(raw.plan_step);
    const total = positiveInteger(raw.plan_total);
    if (step === null || total === null || step > total) {
        return null;
    }

    return { step: step, total: total };
}

// The dollar budget the session runs under, or null without one.
function budgetOf(raw) {
    const used = nonNegativeAmount(raw.budget_used);
    const total = nonNegativeAmount(raw.budget_total);
    if (used === null || total === null || total <= 0) {
        return null;
    }

    return { used: used, total: total, ratio: used / total };
}

// The hooks that failed without blocking, most frequent first as the backend
// sorted them.
function hookErrorsOf(raw) {
    const result = [];
    for (const error of Array.isArray(raw.hook_errors) ? raw.hook_errors : []) {
        if (!error || typeof error.hook !== 'string' || !error.hook) {
            continue;
        }
        const count = positiveInteger(error.count);
        if (count === null) {
            continue;
        }
        const code = Number.isInteger(error.exit_code) && error.exit_code >= 0 ? error.exit_code : null;
        result.push({ hook: error.hook, exit_code: code, count: count });
    }
    return result;
}

// A pull request's address when it is safe to put in an href, else null.
function pullRequestUrl(url) {
    if (typeof url !== 'string' || url.length > PULL_REQUEST_URL_MAX || !PULL_REQUEST_URL.test(url)) {
        return null;
    }
    return url;
}

// The pull requests Claude Code linked to the session, newest first.
function pullRequestsOf(raw) {
    const result = [];
    for (const link of Array.isArray(raw.pr_links) ? raw.pr_links : []) {
        if (!link || typeof link.repository !== 'string' || !link.repository) {
            continue;
        }
        const number = positiveInteger(link.number);
        if (number === null) {
            continue;
        }
        result.push({ repository: link.repository, number: number, url: pullRequestUrl(link.url) });
    }
    return result;
}

function contextLine(context, labels) {
    if (!context) {
        return '';
    }
    if (context.ratio === null) {
        return fmt(labels.context_used, { used: formatTokens(context.used) });
    }
    return fmt(labels.context_tip, {
        used: formatTokens(context.used),
        size: formatTokens(context.size),
        percent: Math.round(context.ratio * 100),
    });
}

// The title's tooltip: the full title, which the row may have cut short, then
// the details that have no column of their own.
function sessionInfoTip(session, labels) {
    const lines = [session.name || ''];
    if (session.branch) {
        lines.push(fmt(labels.branch_tip, { branch: session.branch }));
    }
    const context = contextLine(session.context, labels);
    if (context) {
        lines.push(context);
    }
    if (session.compactions > 0) {
        lines.push(fmt(labels.compactions, { count: session.compactions }));
    }
    if (session.files_edited > 0) {
        lines.push(fmt(labels.files_edited, { count: session.files_edited }));
    }
    if (session.artifacts > 0) {
        lines.push(fmt(labels.artifacts, { count: session.artifacts }));
    }
    return lines.filter(Boolean).join('\n');
}

// The branch most of a panel's sessions are on (the first one seen wins a tie),
// or null when none names one. The header names it once; only a row on another
// branch names its own, so one session elsewhere does not put the shared branch
// on every row. Sessions that name none (a session outside a git repository)
// do not count.
function panelBranch(sessions) {
    const counts = new Map();
    let best = null;
    for (const session of sessions || []) {
        if (!session.branch) {
            continue;
        }
        const count = (counts.get(session.branch) || 0) + 1;
        counts.set(session.branch, count);
        if (best === null || count > counts.get(best)) {
            best = session.branch;
        }
    }
    return best;
}

/* --- project ordering (cross-project attention bands) --- */

// Projects are ordered by attention band, not by fine-grained status: blocked
// first, then busy, then quiet. Fine-grained churn inside a band (working <->
// processing, a ticking token count) never reorders the panels.
//
// Within a band the order is by newest activity (the freshest session's age),
// with the name only as a tie-break. That is what keeps a panel findable when it
// changes band: age grows at the same rate for every panel, so the order shifts
// only when a panel genuinely gains activity (its age drops to ~0 and it rises
// to the top of its band) or crosses a boundary - and a panel that finishes its
// work then slides one position down into the quiet band instead of jumping into
// the middle of a long alphabetical list, where it is effectively lost. An
// alphabetical order inside the band looks stable only until the band changes,
// which is exactly the moment the panel matters most.
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

// A project is as fresh as its most recently active session. An age that is
// missing or did not parse (buildSession floors a non-numeric value to NaN)
// never wins the comparison - a NaN reaching the comparator would make it return
// NaN and leave the whole order undefined - so a project made up of them falls
// back to the alphabetical tie-break.
function projectActivityAge(sessions) {
    let age = Infinity;
    for (const session of sessions || []) {
        const value = session.age_seconds;
        if (Number.isFinite(value) && value < age) {
            age = value;
        }
    }
    return age;
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
// attention bands (needs-you, busy, quiet) and ordered by newest activity within
// each, the name breaking ties; otherwise the list is a plain alphabetical one.
// Both are stable against token and fine-grained status churn - the order
// changes only when a project crosses a band boundary or actually gains
// activity.
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
        const ageA = projectActivityAge(a.sessions);
        const ageB = projectActivityAge(b.sessions);
        // The equality guard is load-bearing, not just symmetry with the band
        // above: two projects with no known age are both Infinity, and the
        // subtraction alone would hand the sort a NaN.
        if (ageA !== ageB) {
            return ageA - ageB;
        }
        return compareProjectsByName(a, b);
    });
}

// Group raw records into projects, per origin and case-insensitive path (see
// groupKey). The group carries its key (the UI's stable panel identity) and,
// for a WSL group, the distro label the panel header shows - a header text
// alone cannot tell two distros' identical paths apart.
function groupProjects(rawSessions, labels, prices) {
    const groups = new Map();
    for (const raw of rawSessions || []) {
        const scratchRoot = scratchWorkspaceRoot(raw.cwd);
        const path = scratchRoot || raw.cwd;
        const key = groupKey(path, raw.origin);
        let group = groups.get(key);
        if (!group) {
            const origin = sessionOrigin(raw);
            group = {
                key: key,
                cwd: displayCwd(path),
                name: scratchRoot ? (labels.project_this_computer || 'This computer') : projectName(raw.cwd),
                origin: origin,
                origin_display: isWslOrigin(origin) ? wslHostLabel(raw.origin_label) : '',
                sessions: [],
            };
            groups.set(key, group);
        }
        group.sessions.push(buildSession(raw, labels, prices));
    }

    // The panel's main branch is named once in the header; a row names its own
    // only when it differs - and not when its worktree badge already carries it.
    for (const group of groups.values()) {
        group.branch = panelBranch(group.sessions) || '';
        for (const session of group.sessions) {
            const inWorktreeBadge = Boolean(session.worktree && session.worktree.branch === session.branch);
            session.show_branch = Boolean(session.branch) && session.branch !== group.branch && !inWorktreeBadge;
        }
    }
    return [...groups.values()];
}

const AMC_LOGIC = {
    fmt,
    esc,
    attr,
    ansiToHtml,
    classify,
    deriveStatus,
    refineWithNative,
    refineWithBackgroundWork,
    refineWithWakeup,
    wakeupPending,
    activeWorkflows,
    needsAttention,
    filterBucket,
    sessionBucket,
    pruneResumedHistory,
    historyNeedsRefresh,
    HISTORY_RANGES,
    DEFAULT_HISTORY_RANGE,
    historyRange,
    historyRangeSeconds,
    historyRangeCovered,
    nextHistoryRange,
    filterHistoryByAge,
    searchScopeRefs,
    searchWideningStep,
    emptyStateMarkup,
    widenedNoticeMarkup,
    sessionMatchesSearch,
    searchHitsMarkup,
    defaultFilterKeys,
    filterSummary,
    sortDirectionOptions,
    sortSummary,
    settleCall,
    pendingIsBlocking,
    pendingBlockReason,
    pendingIsStalled,
    STALLED_PENDING_SECONDS,
    AUTO_STALLED_PENDING_SECONDS,
    PROCESS_START_SLACK_SECONDS,
    turnPredatesProcess,
    turnStopped,
    AUTO_PAUSE_STREAK,
    autoModePaused,
    modeLabel,
    statusLabel,
    apiErrorLabel,
    statusTip,
    attentionLabel,
    refinedStatusLabel,
    statusDetailLines,
    formatAge,
    formatAgeSince,
    formatModel,
    formatTokens,
    tokenLabels,
    resolvePrices,
    modelPriceKey,
    usageCostUsd,
    sessionCostUsd,
    sessionTotalCostUsd,
    webSearchPrice,
    costTipLines,
    ticketKey,
    ticketTotals,
    ticketTip,
    formatCost,
    usageTotalTokens,
    modelHistory,
    cliHistory,
    changelogUrl,
    cliColumnRelevant,
    hostLabel,
    entrypointLabel,
    isViaCli,
    isBackground,
    kindLabelKey,
    isVscodeDeeplink,
    isRemoteControlled,
    groupKey,
    displayCwd,
    projectName,
    scratchWorkspaceRoot,
    modelRank,
    NEAR_LIMIT_RATIO,
    branchOf,
    effortOf,
    isFastMode,
    contextUsage,
    planProgress,
    budgetOf,
    hookErrorsOf,
    pullRequestUrl,
    pullRequestsOf,
    contextLine,
    sessionInfoTip,
    panelBranch,
    buildSession,
    groupProjects,
    projectBand,
    projectActivityAge,
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
