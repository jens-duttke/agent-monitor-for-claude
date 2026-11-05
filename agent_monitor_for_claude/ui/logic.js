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

/* --- list sections ---

   The main list groups sessions by what they ask of you rather than by project.
   Each section owns the filter buckets whose sessions it shows, so collapsing a
   section is switching those buckets off: the persisted selection, the search
   scope and the widening offer keep working off the same keys they always did. */

// Most urgent first. `order` names how a section sorts its sessions (see
// sortSectionSessions): the queue of sessions blocked on you by how long each
// has waited, the running ones by when their process started - their ages
// change every few seconds, and a list that reorders under the pointer cannot be
// clicked - and the rest newest first.
const LIST_SECTIONS = [
    { key: 'needs', label: 'section_needs', buckets: ['needs'], order: 'waiting' },
    { key: 'stopped', label: 'section_stopped', buckets: ['errored', 'interrupted'], order: 'newest' },
    { key: 'running', label: 'section_running', buckets: ['working', 'background'], order: 'started' },
    { key: 'recent', label: 'section_recent', buckets: ['idle', 'new', 'quiet'], order: 'newest' },
    { key: 'older', label: 'section_older', buckets: ['history'], order: 'newest' },
];

function listSection(key) {
    return LIST_SECTIONS.find((section) => section.key === key) || null;
}

// The section a session belongs to, or null for a status no bucket knows.
function sectionOf(session) {
    const bucket = sessionBucket(session);
    const section = LIST_SECTIONS.find((candidate) => candidate.buckets.includes(bucket));
    return section ? section.key : null;
}

// A section is open while any of its buckets is active. A selection persisted
// before the sections existed can hold only some of them; such a section reads
// open and shows the sessions of the buckets still on.
function sectionActive(sectionKey, activeKeys) {
    const section = listSection(sectionKey);
    const active = asKeySet(activeKeys);
    return Boolean(section) && section.buckets.some((bucket) => active.has(bucket));
}

// The active filter keys after a click on a section: an open one closes with
// all its buckets, a closed one opens with all of them.
function toggledSectionKeys(sectionKey, activeKeys) {
    const section = listSection(sectionKey);
    const next = new Set(asKeySet(activeKeys));
    if (!section) {
        return next;
    }
    const open = sectionActive(sectionKey, next);
    for (const bucket of section.buckets) {
        if (open) {
            next.delete(bucket);
        } else {
            next.add(bucket);
        }
    }
    return next;
}

// A finite number, or null. `Number(null)` is 0, so null is checked first.
// A number from on-disk data, or null: only a finite number or a string that
// holds one counts - Number() reads '', [] and a blank string as 0, which would
// pass "unknown" off as a real zero.
function finiteOrNull(value) {
    let number = null;
    if (typeof value === 'number') {
        number = value;
    } else if (typeof value === 'string' && value.trim() !== '') {
        number = Number(value);
    }
    return number !== null && Number.isFinite(number) ? number : null;
}

// Name, then session id, then pid: the tie-break that keeps every order total,
// so two sessions with equal keys never swap places between polls.
function compareSessionIdentity(a, b) {
    const nameA = String(a.name || '').toLowerCase();
    const nameB = String(b.name || '').toLowerCase();
    if (nameA !== nameB) {
        return nameA < nameB ? -1 : 1;
    }
    const idA = String(a.session_id || '');
    const idB = String(b.session_id || '');
    if (idA !== idB) {
        return idA < idB ? -1 : 1;
    }
    return (Number(a.pid) || 0) - (Number(b.pid) || 0);
}

// Ascending on a nullable number, a missing value last.
function compareNullable(a, b) {
    if (a === b) {
        return 0;
    }
    if (a === null) {
        return 1;
    }
    if (b === null) {
        return -1;
    }
    return a - b;
}

// Sort one section's sessions. `waiting` puts the longest wait first: a session
// blocked on you writes nothing while it waits, so its age is the wait. `started`
// orders by process start, most recent first. `newest` orders by last activity.
// Every order grows all keys at the same rate, so it only changes when a session
// genuinely does something.
function sortSectionSessions(sessions, order) {
    const keyOf = (session) => {
        if (order === 'started') {
            return finiteOrNull(session.process_age_seconds);
        }
        const age = finiteOrNull(session.age_seconds);
        return order === 'waiting' && age !== null ? -age : age;
    };
    return [...(sessions || [])].sort((a, b) => compareNullable(keyOf(a), keyOf(b)) || compareSessionIdentity(a, b));
}

// The sessions of one project group: most urgent status first, then
// newest activity.
function sortGroupSessions(sessions) {
    return [...(sessions || [])].sort((a, b) => {
        const rankA = STATUS_ORDER[a.status] ?? 99;
        const rankB = STATUS_ORDER[b.status] ?? 99;
        if (rankA !== rankB) {
            return rankA - rankB;
        }
        return compareNullable(finiteOrNull(a.age_seconds), finiteOrNull(b.age_seconds)) || compareSessionIdentity(a, b);
    });
}

// Split sessions into the list sections, in section order, each sorted. A
// section with no session is still returned (empty), so a closed section keeps
// its header and a count of zero reads as "nothing here" rather than vanishing.
function buildSections(sessions, activeKeys) {
    const bySection = new Map(LIST_SECTIONS.map((section) => [section.key, []]));
    for (const session of sessions || []) {
        const key = sectionOf(session);
        if (key) {
            bySection.get(key).push(session);
        }
    }
    return LIST_SECTIONS.map((section) => ({
        key: section.key,
        label: section.label,
        active: sectionActive(section.key, activeKeys),
        sessions: sortSectionSessions(bySection.get(section.key), section.order),
    }));
}

// Every session of every project as one list, each carrying the project it was
// grouped under - the sections cut across projects, and a
// list entry still names its project.
function flattenProjects(projects) {
    const result = [];
    for (const project of projects || []) {
        for (const session of project.sessions || []) {
            result.push(Object.assign({}, session, {
                project_key: project.key,
                project_name: project.name,
                project_cwd: project.cwd,
                project_origin: project.origin,
                project_origin_display: project.origin_display || '',
            }));
        }
    }
    return result;
}

/* --- signals ---

   The few facts a list entry spells out beside its status, so the list answers
   "what is going on there" without opening the session. Ranked by how much each
   changes what you do next; the list shows the first two, the detail all. */

const LIST_SIGNAL_LIMIT = 2;

function sessionSignals(session, labels) {
    const strings = labels || {};
    const signals = [];
    const push = (kind, tone, text) => {
        if (text) {
            signals.push({ kind, tone, text });
        }
    };

    if (session.processes > 0) {
        push('processes', 'work', fmt(strings.signal_processes, { count: session.processes }));
    }
    if (session.workflow_active) {
        push('workflow', 'background', fmt(strings.signal_workflow, { done: session.workflow_done, total: session.workflow_total }));
    } else if (session.subagents_running > 0) {
        push('agents', 'background', fmt(strings.signal_agents, { count: session.subagents_running }));
    }
    if (session.context_warn) {
        push('context', 'warn', fmt(strings.signal_context, { percent: Math.round(session.context.ratio * 100) }));
    }
    if (session.budget && session.budget.ratio >= NEAR_LIMIT_RATIO) {
        push('budget', 'warn', fmt(strings.signal_budget, { percent: Math.round(session.budget.ratio * 100) }));
    }
    if (session.cache_warn) {
        push('cache', 'warn', fmt(strings.signal_cache, { cost: formatCost(session.cache.rewrite_usd) }));
    }
    if (session.auto_paused) {
        push('auto-paused', 'warn', strings.signal_auto_paused);
    }
    if (session.cli_update) {
        push('update', 'plain', fmt(strings.signal_update, { version: session.cli_update.available }));
    }
    const hookErrors = (session.hook_errors || []).reduce((sum, error) => sum + error.count, 0);
    if (hookErrors > 0) {
        push('hooks', 'warn', fmt(strings.signal_hook_errors, { count: hookErrors }));
    }
    if (session.queued > 0) {
        push('queued', 'plain', fmt(strings.signal_queued, { count: session.queued }));
    }
    return signals;
}

/* --- search over what the list already shows ---

   Matching a session's own title, project, branch and id needs no file access,
   so it filters while the query is typed; the content search runs beside it and
   adds what only the transcripts hold. */

// The query as a RegExp under the editor options, or null when it cannot be
// one (an invalid pattern in regex mode). The pattern is the user's own input,
// used for matching only.
function searchRegExp(query, options) {
    const opts = options || {};
    const text = String(query == null ? '' : query).trim();
    if (!text) {
        return null;
    }
    let source = opts.use_regex ? text : text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (opts.whole_word) {
        source = '\\b(?:' + source + ')\\b';
    }
    try {
        return new RegExp(source, opts.match_case ? '' : 'i');
    } catch (error) {
        return null;
    }
}

// Which of the session's own fields the query matches - 'title', 'project',
// 'branch' or 'id' - or null. An id matches from its start only, so a pasted
// id or its first characters find the session, while a stray hex digit does not.
function sessionMetaMatch(session, query, options) {
    const text = String(query == null ? '' : query).trim();
    if (!session || !text) {
        return null;
    }
    const pattern = searchRegExp(text, options);
    if (pattern) {
        const fields = [
            ['title', session.name],
            ['project', session.project_name],
            ['branch', session.branch],
            ['branch', session.worktree ? session.worktree.branch : ''],
        ];
        for (const [field, value] of fields) {
            if (typeof value === 'string' && value && pattern.test(value)) {
                return field;
            }
        }
    }
    const id = typeof session.session_id === 'string' ? session.session_id.toLowerCase() : '';
    if (id && text.length >= 4 && id.startsWith(text.toLowerCase())) {
        return 'id';
    }
    return null;
}

// Whether a content search counts a session as found. Only a result that
// arrived counts: a null match set (a query still inside its typing debounce,
// or a cleared or invalid one) finds nothing, an empty Map means it ran and
// found nothing. The session's own fields are matched separately
// (sessionMetaMatch), which is what keeps the list from going blank while the
// scan is still to come.
function sessionMatchesSearch(sessionId, searchActive, searchMatches) {
    if (!searchActive) {
        return true;
    }
    return searchMatches != null && searchMatches.has(sessionId);
}

/* --- taskbar attention --- */

// The sessions blocked on you that the taskbar counts, and whether one of them
// is new since the previous report - only a newcomer makes the button flash,
// never a session that was already waiting. `previousKeys` null means there was
// no previous report: the first one sets the count without flashing.
function attentionUpdate(previousKeys, sessions) {
    const keys = new Set();
    for (const session of sessions || []) {
        if (session && !session.is_history && session.status === 'awaiting_permission') {
            keys.add((session.session_id || '') + '#' + session.pid);
        }
    }
    let newlyWaiting = false;
    if (previousKeys instanceof Set) {
        for (const key of keys) {
            if (!previousKeys.has(key)) {
                newlyWaiting = true;
                break;
            }
        }
    }
    return { count: keys.size, keys, newlyWaiting };
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
//
// `maxSnippets` shows only the first few excerpts - a list entry has room for
// one, the detail for all of them - and the rest counts as "not shown".
function searchHitsMarkup(hit, labels, maxSnippets) {
    const count = hit ? Number(hit.count) : 0;
    if (!Number.isFinite(count) || count <= 0) {
        return '';
    }

    const strings = labels || {};
    const allSnippets = Array.isArray(hit.snippets) ? hit.snippets : [];
    const snippets = Number.isInteger(maxSnippets) && maxSnippets >= 0 ? allSnippets.slice(0, maxSnippets) : allSnippets;
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
        const rate = modelRate(modelId, table);
        if (!rate) {
            return null;
        }
        total += usageCostUsd(usage, rate);
    }
    return total;
}

// A model's rates in the resolved price table, or null without one.
function modelRate(modelId, prices) {
    const table = prices || {};
    const key = modelPriceKey(modelId);
    // Own-property check: a key like "constructor"/"toString" would otherwise
    // resolve to an inherited Object.prototype member (truthy) and price the
    // model at $0 instead of falling back to the token total. modelId comes
    // from untrusted on-disk transcripts.
    const rate = key && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;
    return isRecord(rate) ? rate : null;
}

/* --- prompt cache outlook ---

   Every request reads the conversation so far from the prompt cache and restarts
   its lifetime (5 minutes or 1 hour, whichever the last write used). A session
   left alone past that lifetime pays for its whole context again on the next
   request, at the cache-write rate instead of the read rate. */

// From this cost on, a lapsed cache earns a badge on the row; below it, and
// while the cache is warm, the line stays in the cost tooltip.
const CACHE_REWRITE_BADGE_USD = 1;

const SECONDS_PER_HOUR = 3600;

// Whether the session's prompt cache is still warm and for how long, how many
// tokens the next request sends, and what writing them again would cost - null
// for the cost when the model has no write rate. Null when the cache's lifetime,
// the context or the deadline is unknown.
function cacheOutlook(raw, prices) {
    const ttl = positiveInteger(raw.cache_ttl_seconds);
    const tokens = positiveInteger(raw.context_tokens);
    const remaining = raw.cache_expires_in_seconds == null || typeof raw.cache_expires_in_seconds === 'boolean'
        ? NaN : Number(raw.cache_expires_in_seconds);
    if (ttl === null || tokens === null || !Number.isFinite(remaining)) {
        return null;
    }

    const rate = modelRate(raw.model_id, prices);
    const writeRate = rate ? (ttl >= SECONDS_PER_HOUR ? rate.cache_write_1h : rate.cache_write_5m) : null;
    const priced = typeof writeRate === 'number' && Number.isFinite(writeRate) && writeRate >= 0;

    return {
        warm: remaining > 0,
        remaining_seconds: Math.max(0, remaining),
        tokens: tokens,
        rewrite_usd: priced ? tokens * writeRate / TOKENS_PER_UNIT_PRICE : null,
    };
}

function cacheLine(cache, labels) {
    if (!cache) {
        return '';
    }
    if (cache.warm) {
        return fmt(labels.cache_warm_tip, { time: formatAge(cache.remaining_seconds, labels) });
    }
    if (cache.rewrite_usd === null) {
        return fmt(labels.cache_cold_tokens_tip, { tokens: formatTokens(cache.tokens) });
    }
    return fmt(labels.cache_cold_tip, { tokens: formatTokens(cache.tokens), cost: formatCost(cache.rewrite_usd) });
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

// What the main conversation and the subagents each cost, for the stacked bar
// in the detail: null when the session ran no subagent (nothing to split) or a
// part cannot be priced.
function costShares(raw, prices) {
    const parts = raw ? raw.usage_parts : null;
    if (!isRecord(parts)) {
        return null;
    }
    const agents = isRecord(parts.agents) ? parts.agents : {};
    const agentUsage = mergedModelUsage(Object.keys(agents).map((name) => agents[name]));
    if (modelUsageTokens(agentUsage) === 0) {
        return null;
    }
    const main = sessionCostUsd(parts.main, prices);
    const subagents = sessionCostUsd(agentUsage, prices);
    if (main === null || subagents === null) {
        return null;
    }
    return { main: main, agents: subagents };
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

/* --- view assembly --- */

// The main conversation's model-switch timeline, oldest first: one entry per
// contiguous run of a model, each carrying the moment that run began. Already
// ordered and run-compressed by the backend, so a model left and returned to
// appears more than once and the last entry is the current model. Feeds the
// detail's model history when more than one run occurred. Timestamps stay raw
// ISO - the UI formats them.
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

// A Claude Code version as its list of numbers, or null for anything that is not
// dot-separated digits - the value comes from disk and must not be guessed at.
const RELEASE_VERSION_PATTERN = /^\d{1,6}(\.\d{1,6}){1,3}$/;

function parseVersion(text) {
    if (typeof text !== 'string' || !RELEASE_VERSION_PATTERN.test(text)) {
        return null;
    }
    return text.split('.').map(Number);
}

// Numeric, part by part, so 2.1.318 is newer than 2.1.99; a missing part counts as 0.
function compareVersions(a, b) {
    const length = Math.max(a.length, b.length);
    for (let index = 0; index < length; index++) {
        const difference = (a[index] || 0) - (b[index] || 0);
        if (difference !== 0) {
            return difference;
        }
    }
    return 0;
}

// The entrypoints that run the Claude Code installed on the machine, and so pick
// up its self-update when restarted. VS Code and Claude Desktop bring their own.
const INSTALLED_CLI_ENTRYPOINTS = new Set(['cli']);

// Which sessions share a Claude Code: one root, one app - and for an app that
// brings its own copy, one host, since VS Code, its Insiders build and Cursor
// update their extensions independently. Null for a record that names no app,
// which therefore has nothing to be compared with.
function cliUpdateGroup(raw) {
    if (typeof raw.entrypoint !== 'string' || !raw.entrypoint) {
        return null;
    }
    const host = INSTALLED_CLI_ENTRYPOINTS.has(raw.entrypoint) ? '' : (typeof raw.host === 'string' ? raw.host : '');
    return sessionOrigin(raw) + '\n' + raw.entrypoint + '\n' + host;
}

// Per group (see cliUpdateGroup), the newest version a live session runs - and,
// for the CLI, the version its root has installed - as {parts, text}.
function newestCliVersions(rawSessions) {
    const newest = new Map();
    const consider = (group, text) => {
        const parts = parseVersion(text);
        const current = newest.get(group);
        if (parts && (!current || compareVersions(parts, current.parts) > 0)) {
            newest.set(group, { parts: parts, text: text });
        }
    };

    for (const raw of rawSessions || []) {
        const group = raw && raw.alive === true ? cliUpdateGroup(raw) : null;
        if (group === null) {
            continue;
        }
        consider(group, raw.process_version);
        if (INSTALLED_CLI_ENTRYPOINTS.has(raw.entrypoint)) {
            consider(group, raw.installed_version);
        }
    }
    return newest;
}

// The newer Claude Code a live session would run after a restart, as
// {running, available}, or null when it is already on the newest one its group
// knows of. Only the process's own version counts: the transcript's may belong
// to an earlier process of a reopened session.
function cliUpdateFor(raw, newest) {
    if (!raw || raw.alive !== true) {
        return null;
    }
    const running = parseVersion(raw.process_version);
    const group = cliUpdateGroup(raw);
    const reference = group === null ? null : newest.get(group);
    if (!running || !reference || compareVersions(reference.parts, running) <= 0) {
        return null;
    }
    return { running: raw.process_version, available: reference.text };
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
    // Only a lapsed cache the next prompt of yours would pay for is worth saying:
    // a running turn keeps its cache warm, and an ended session has none.
    const cache = alive && needsAttention(status) ? cacheOutlook(raw, prices) : null;
    const costLines = (raw.scan_pending ? [] : costTipLines(raw, prices, labels)).concat(cacheLine(cache, labels));

    // The call in flight belongs to the process that made it: a stopped turn
    // or a reopened session has none, whatever the transcript's tail says.
    const call = alive && !stopped ? currentToolOf(raw) : null;

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
        // A newer Claude Code a restart would bring; set by groupProjects.
        cli_update: null,
        usage_compact: usageCompact,
        usage_detail: usageDetail,
        usage_total: usageTotalTokens(usage),
        // The breakdown is unknown while a scan is pending, like the figure it
        // explains; the cache line comes from the tail and is known at once.
        cost_tip: costLines.filter(Boolean).join('\n'),
        // The estimate as a number, for group sums; null while the first scan
        // runs or when a model has no rate.
        cost_usd: raw.scan_pending ? null : sessionTotalCostUsd(raw, prices),
        cost_shares: raw.scan_pending ? null : costShares(raw, prices),
        // The first full scan still runs in the background; selecting the
        // session moves it to the front of that queue.
        scan_pending: Boolean(raw.scan_pending),
        subagents_running: subagentsRunning,
        subagents_done: raw.subagents_done || 0,
        subagents_labels: subagentsLabels,
        workflow_active: workflows.length > 0,
        workflow_total: workflowTotal,
        workflow_done: workflowDone,
        processes: raw.child_count || 0,
        process_list: processesOf(raw),
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
        // Where git runs for the session: the worktree it switched into, if any.
        worktree_cwd: typeof raw.worktree_cwd === 'string' ? raw.worktree_cwd : '',
        branch: branchOf(raw),
        // Whether the row names the branch itself; groupProjects sets it when the
        // panel's sessions are on different branches.
        show_branch: false,
        effort: effortOf(raw),
        fast_mode: isFastMode(raw),
        context: context,
        context_warn: Boolean(context && context.ratio !== null && context.ratio >= NEAR_LIMIT_RATIO),
        cache: cache,
        cache_warn: Boolean(cache && !cache.warm && cache.rewrite_usd !== null && cache.rewrite_usd >= CACHE_REWRITE_BADGE_USD),
        plan: alive ? planProgress(raw) : null,
        queued: QUEUE_STATUSES.has(status) ? (positiveInteger(raw.queued) || 0) : 0,
        budget: budgetOf(raw),
        hook_errors: alive ? hookErrorsOf(raw) : [],
        current_tool: call,
        activity: activityOf(raw, status, call, labels),
        last_reply: typeof raw.last_reply === 'string' ? raw.last_reply : '',
        plan_items: alive ? planItemsOf(raw) : [],
        lines: linesOf(raw),
        work: workSummary(raw, status),
        agents: agentsOf(raw, prices),
        messages: messagesOf(raw),
        tools: toolsOf(raw.tools),
        pull_requests: pullRequestsOf(raw),
        files_edited: positiveInteger(raw.files_edited) || 0,
        compactions: positiveInteger(raw.compactions) || 0,
        artifacts: positiveInteger(raw.artifact_count) || 0,
        artifact_links: artifactsOf(raw),
        age_seconds: raw.age_seconds == null ? null : Math.floor(raw.age_seconds),
        // How long the process holding the session has run - the stable order of
        // the running sessions, whose activity ages change every few seconds.
        process_age_seconds: finiteOrNull(raw.process_age_seconds),
        alive: alive,
    };
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
// near. Below it the figure shows in the detail only.
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

// The prefix of the branch Claude Desktop creates for a chat it starts in a
// git worktree of its own, followed by that worktree's folder name.
const DESKTOP_WORKTREE_BRANCH_PREFIX = 'claude/';

// The last segment of a Windows or POSIX path, or ''.
function lastPathSegment(path) {
    if (typeof path !== 'string') {
        return '';
    }
    const segments = path.split(/[\\/]/).filter(Boolean);
    return segments.length > 0 ? segments[segments.length - 1] : '';
}

// The current git branch, or '' when the transcript names none - or when it is
// only the branch Claude Desktop named after the worktree folder the session
// works in, which the folder path already says. Once the agent switches to a
// branch of its own, that one is shown.
function branchOf(raw) {
    if (typeof raw.git_branch !== 'string') {
        return '';
    }
    for (const folder of [lastPathSegment(raw.cwd), lastPathSegment(raw.worktree_cwd)]) {
        if (folder && raw.git_branch === DESKTOP_WORKTREE_BRANCH_PREFIX + folder) {
            return '';
        }
    }
    return raw.git_branch;
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
        const message = typeof error.message === 'string' ? error.message : '';
        result.push({ hook: error.hook, exit_code: code, count: count, message: message });
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

// The artifacts the session published, in order of publication. An address
// reaches an href only through pullRequestUrl's check - otherwise the artifact
// is listed without a link - and a missing title falls back to the address.
function artifactsOf(raw) {
    const result = [];
    for (const artifact of Array.isArray(raw.artifacts) ? raw.artifacts : []) {
        if (!artifact || typeof artifact.url !== 'string' || !artifact.url) {
            continue;
        }
        const title = typeof artifact.title === 'string' && artifact.title.trim() ? artifact.title : artifact.url;
        result.push({ title: title, url: pullRequestUrl(artifact.url) });
    }
    return result;
}

/* --- what a session runs, waits on and last said ---

   The transcript names the call a session runs or waits on (a command, a
   question with its answers, a plan to review) and the last reply it gave.
   These pick the one line the list shows and the blocks the detail shows. */

const SUBAGENT_TOOLS = new Set(['Agent', 'Task']);
// A status during which the call is being executed rather than waited on.
const ACTIVITY_RUNNING = new Set(['working', 'processing']);
// The statuses whose list line is the last reply: the turn is over.
const ACTIVITY_REPLY = new Set(['awaiting_input', 'completed', 'interrupted', 'unknown']);
const PLAN_ITEM_STATUSES = new Set(['pending', 'in_progress', 'completed']);

function firstLine(text) {
    if (typeof text !== 'string') {
        return '';
    }
    for (const line of text.split('\n')) {
        const trimmed = line.trim();
        if (trimmed) {
            return trimmed;
        }
    }
    return '';
}

// "mcp__server__tool" -> "tool"; a built-in tool keeps its name.
function toolDisplayName(tool) {
    if (typeof tool !== 'string' || !tool) {
        return '';
    }
    const mcp = tool.match(/^mcp__(.+?)__(.+)$/);
    return mcp ? mcp[2] : tool;
}

// The call the session runs or waits on, from the raw record; null without one.
function currentToolOf(raw) {
    const call = raw ? raw.current_tool : null;
    if (!call || typeof call !== 'object' || typeof call.tool !== 'string' || !call.tool) {
        return null;
    }
    const questions = [];
    for (const question of Array.isArray(call.questions) ? call.questions : []) {
        if (!question || typeof question.text !== 'string' || !question.text) {
            continue;
        }
        const options = (Array.isArray(question.options) ? question.options : []).filter((option) => typeof option === 'string' && option);
        questions.push({ text: question.text, options: options });
    }
    return {
        tool: call.tool,
        name: toolDisplayName(call.tool),
        text: typeof call.text === 'string' ? call.text : '',
        detail: typeof call.detail === 'string' ? call.detail : '',
        questions: questions,
        age_seconds: finiteOrNull(raw.current_tool_age_seconds),
        plan: PLAN_TOOLS.has(call.tool),
    };
}

// One line naming a call by what it acts on: "Bash · npm test".
function toolCallLine(call) {
    if (!call) {
        return '';
    }
    let subject = call.text;
    if (SUBAGENT_TOOLS.has(call.tool) && call.detail) {
        subject = subject ? subject + ': ' + call.detail : call.detail;
    }
    subject = firstLine(subject) || firstLine(call.detail);
    return subject ? call.name + ' · ' + subject : call.name;
}

// The one line the list shows under a session's title: what it waits on, what
// it runs (with since when), or what it last said. Null when there is nothing
// to say. `call` is the session's current call (see buildSession).
function activityOf(raw, status, call, labels) {
    const strings = labels || {};
    if (status === 'awaiting_permission' && call) {
        if (call.questions.length > 0) {
            const more = call.questions.length > 1 ? ' (+' + (call.questions.length - 1) + ')' : '';
            return { kind: 'ask', text: call.questions[0].text + more, since: null };
        }
        if (call.plan) {
            return { kind: 'plan', text: firstLine(call.text) || strings.activity_plan || '', since: null };
        }
        return { kind: 'tool', text: toolCallLine(call), since: null };
    }
    if (ACTIVITY_RUNNING.has(status) && call) {
        return { kind: 'tool', text: toolCallLine(call), since: call.age_seconds };
    }
    const reply = firstLine(raw.last_reply);
    if (reply && ACTIVITY_REPLY.has(status)) {
        return { kind: 'reply', text: reply, since: null };
    }
    return null;
}

// The plan's steps with their texts, as Claude Desktop's plan tool set them.
function planItemsOf(raw) {
    const result = [];
    for (const item of Array.isArray(raw.plan_items) ? raw.plan_items : []) {
        if (!item || typeof item.text !== 'string' || !item.text) {
            continue;
        }
        result.push({ text: item.text, status: PLAN_ITEM_STATUSES.has(item.status) ? item.status : 'pending' });
    }
    return result;
}

// The lines the session's edits added and removed; null before the first scan
// or when it changed nothing.
function linesOf(raw) {
    if (raw.scan_pending) {
        return null;
    }
    const added = positiveInteger(raw.lines_added) || 0;
    const removed = positiveInteger(raw.lines_removed) || 0;
    return added > 0 || removed > 0 ? { added: added, removed: removed } : null;
}

/* --- worked and waiting time ---

   A worked interval runs from a prompt to the turn's final reply. Within the
   last day the time between them is the time the session waited - for you, or
   for nothing once it was done. */

const WORK_WINDOW_SECONDS = 86400;

function workSummary(raw, status) {
    const spans = [];
    for (const pair of Array.isArray(raw.work_intervals) ? raw.work_intervals : []) {
        if (!Array.isArray(pair) || pair.length !== 2) {
            continue;
        }
        const start = finiteOrNull(pair[0]);
        const end = finiteOrNull(pair[1]);
        if (start === null || end === null || end < 0 || start < end || end >= WORK_WINDOW_SECONDS) {
            continue;
        }
        spans.push([Math.min(start, WORK_WINDOW_SECONDS), end]);
    }
    const alive = raw.alive === true;
    // The turn still running counts up to now while the model works, and up to
    // its newest entry while it waits on a dialog. It cannot have started
    // before its process did: a turn an earlier process left open and a prompt
    // after the reopen continued is counted from the reopen.
    const opened = alive && !turnStopped(raw) ? finiteOrNull(raw.turn_open_seconds) : null;
    const processAge = finiteOrNull(raw.process_age_seconds);
    const open = opened !== null && processAge !== null ? Math.min(opened, processAge) : opened;
    if (open !== null && open < WORK_WINDOW_SECONDS * 2) {
        const idle = finiteOrNull(raw.age_seconds);
        if (ACTIVITY_RUNNING.has(status)) {
            spans.push([Math.min(open, WORK_WINDOW_SECONDS), 0]);
        } else if (status === 'awaiting_permission' && idle !== null && open > idle && idle < WORK_WINDOW_SECONDS) {
            spans.push([Math.min(open, WORK_WINDOW_SECONDS), idle]);
        }
    }
    if (spans.length === 0) {
        return null;
    }

    spans.sort((a, b) => b[0] - a[0]);
    const merged = [];
    for (const [start, end] of spans) {
        const last = merged[merged.length - 1];
        if (last && start >= last[1]) {
            last[1] = Math.min(last[1], end);
        } else {
            merged.push([start, end]);
        }
    }
    const spanStart = merged[0][0];
    const spanEnd = alive ? 0 : Math.min(...merged.map((span) => span[1]));
    const total = spanStart - spanEnd;
    if (!(total > 0)) {
        return null;
    }
    const work = merged.reduce((sum, [start, end]) => sum + (start - end), 0);
    return {
        work_seconds: work,
        wait_seconds: Math.max(0, total - work),
        span_seconds: total,
        span_start_seconds: spanStart,
        turns: spans.length,
        ends_now: alive,
        segments: merged.map(([start, end]) => ({ left: (spanStart - start) / total, width: (start - end) / total })),
    };
}

/* --- test results of a background task --- */

// "✓ 42 passed" or "✗ 3 failed · 39 passed", from the counts the backend parsed
// out of a task's output; null for a task that printed no summary.
function testsLabel(tests, labels) {
    if (!tests || typeof tests !== 'object') {
        return null;
    }
    const strings = labels || {};
    const passed = Number.isInteger(tests.passed) && tests.passed >= 0 ? tests.passed : 0;
    const failed = Number.isInteger(tests.failed) && tests.failed >= 0 ? tests.failed : 0;
    const skipped = Number.isInteger(tests.skipped) && tests.skipped >= 0 ? tests.skipped : 0;
    const parts = [];
    if (failed > 0) {
        parts.push(fmt(strings.tests_failed, { count: failed }));
    }
    parts.push(fmt(strings.tests_passed, { count: passed }));
    if (skipped > 0) {
        parts.push(fmt(strings.tests_skipped, { count: skipped }));
    }
    return { ok: failed === 0, text: (failed === 0 ? '✓ ' : '✗ ') + parts.join(' · ') };
}

/* --- Markdown, for the texts the details show whole ---

   A small renderer for the Markdown Claude writes: headings, paragraphs,
   emphasis, inline and fenced code, lists (nested, with task boxes), quotes,
   tables and rules. Every piece of text passes through esc, and the only
   markup is the fixed set of elements written here. A link becomes its text,
   with its address as a tooltip (attr) - never an href, since the address is
   conversation text. */

const MD_FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const MD_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const MD_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MD_QUOTE = /^\s{0,3}>\s?(.*)$/;
const MD_ITEM = /^( *)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const MD_TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
const MD_TASK = /^\[([ xX])\]\s+/;
const MD_INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\[([^\]\n]+)\]\(([^()\s]+)\)/g;
// Quotes and lists inside quotes and lists render as text past this depth.
const MD_MAX_DEPTH = 8;

function markdownToHtml(text) {
    const lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n');
    return mdBlocks(lines, 0);
}

function mdBlocks(lines, depth) {
    if (depth > MD_MAX_DEPTH) {
        return '<p>' + mdInline(lines.join('\n')) + '</p>';
    }
    let html = '';
    let paragraph = [];
    const flush = () => {
        if (paragraph.length > 0) {
            html += '<p>' + mdInline(paragraph.join('\n')) + '</p>';
            paragraph = [];
        }
    };
    let index = 0;
    while (index < lines.length) {
        const line = lines[index];
        if (!line.trim()) {
            flush();
            index += 1;
            continue;
        }

        const fence = MD_FENCE.exec(line);
        if (fence) {
            flush();
            const body = [];
            index += 1;
            while (index < lines.length && !mdClosesFence(lines[index], fence[1])) {
                body.push(lines[index]);
                index += 1;
            }
            index += 1;
            html += '<pre class="md-code"><code>' + esc(body.join('\n')) + '</code></pre>';
            continue;
        }

        const heading = MD_HEADING.exec(line);
        if (heading) {
            flush();
            const level = heading[1].length;
            html += '<h' + level + '>' + mdInline(heading[2]) + '</h' + level + '>';
            index += 1;
            continue;
        }

        if (MD_RULE.test(line)) {
            flush();
            html += '<hr>';
            index += 1;
            continue;
        }

        if (MD_QUOTE.test(line)) {
            flush();
            const body = [];
            while (index < lines.length && MD_QUOTE.test(lines[index])) {
                body.push(MD_QUOTE.exec(lines[index])[1]);
                index += 1;
            }
            html += '<blockquote>' + mdBlocks(body, depth + 1) + '</blockquote>';
            continue;
        }

        if (line.includes('|') && index + 1 < lines.length && lines[index + 1].includes('-') && MD_TABLE_RULE.test(lines[index + 1])) {
            flush();
            const table = mdTable(lines, index);
            html += table.html;
            index = table.next;
            continue;
        }

        if (MD_ITEM.test(line)) {
            flush();
            const list = mdList(lines, index, depth);
            html += list.html;
            index = list.next;
            continue;
        }

        paragraph.push(line);
        index += 1;
    }
    flush();
    return html;
}

// Whether `line` closes a fence opened by `marker`: the same character, at
// least as many of them, and nothing else on the line.
function mdClosesFence(line, marker) {
    const trimmed = line.trim();
    return trimmed.length >= marker.length && trimmed === marker[0].repeat(trimmed.length);
}

function mdLeadingSpaces(line) {
    let count = 0;
    while (count < line.length && line[count] === ' ') {
        count += 1;
    }
    return count;
}

function mdDedent(line, width) {
    return line.slice(Math.min(width, mdLeadingSpaces(line)));
}

// A list from lines[start]: its items at that indentation and of that kind,
// each with the lines indented under it (a nested list, a second paragraph).
function mdList(lines, start, depth) {
    const first = MD_ITEM.exec(lines[start]);
    const indent = first[1].length;
    const ordered = /\d/.test(first[2]);
    const items = [];
    let index = start;
    while (index < lines.length) {
        const match = MD_ITEM.exec(lines[index]);
        if (!match || match[1].length !== indent || /\d/.test(match[2]) !== ordered) {
            break;
        }
        const width = indent + match[2].length + 1;
        const body = [match[3]];
        index += 1;
        while (index < lines.length) {
            const next = lines[index];
            if (!next.trim()) {
                let ahead = index + 1;
                while (ahead < lines.length && !lines[ahead].trim()) {
                    ahead += 1;
                }
                if (ahead < lines.length && mdLeadingSpaces(lines[ahead]) > indent) {
                    body.push('');
                    index += 1;
                    continue;
                }
                break;
            }
            if (mdLeadingSpaces(next) > indent) {
                body.push(mdDedent(next, width));
                index += 1;
                continue;
            }
            // A line that starts nothing new continues the item's text.
            const startsBlock = MD_ITEM.test(next) || MD_FENCE.test(next) || MD_HEADING.test(next) || MD_QUOTE.test(next) || MD_RULE.test(next);
            if (!startsBlock && body[body.length - 1].trim()) {
                body.push(next);
                index += 1;
                continue;
            }
            break;
        }
        items.push(body);
    }

    const tag = ordered ? 'ol' : 'ul';
    const number = parseInt(first[2], 10);
    let html = '<' + tag + (ordered && number !== 1 ? attr('start', number) : '') + '>';
    for (const body of items) {
        const task = MD_TASK.exec(body[0]);
        if (task) {
            body[0] = body[0].slice(task[0].length);
        }
        const box = task ? '<span class="md-box">' + (task[1] === ' ' ? '☐' : '☑') + '</span>' : '';
        html += '<li' + (task ? ' class="md-task"' : '') + '>' + box + mdBlocks(body, depth + 1) + '</li>';
    }
    return { html: html + '</' + tag + '>', next: index };
}

// A table from its head line at lines[start], its rule line and the rows under it.
function mdTable(lines, start) {
    const head = mdCells(lines[start]);
    const aligns = mdCells(lines[start + 1]).map((cell) => {
        if (cell.startsWith(':') && cell.endsWith(':')) {
            return 'center';
        }
        return cell.endsWith(':') ? 'right' : '';
    });
    let index = start + 2;
    const rows = [];
    while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(mdCells(lines[index]));
        index += 1;
    }
    const cell = (tag, text, align) => '<' + tag + (align ? attr('class', 'md-' + align) : '') + '>' + mdInline(text) + '</' + tag + '>';
    const html = '<div class="md-table"><table><thead><tr>' + head.map((text, column) => cell('th', text, aligns[column])).join('') + '</tr></thead>'
        + '<tbody>' + rows.map((row) => '<tr>' + head.map((_, column) => cell('td', row[column] || '', aligns[column])).join('') + '</tr>').join('')
        + '</tbody></table></div>';
    return { html: html, next: index };
}

// A table row's cells. A pipe separates cells unless it is escaped or sits in
// inline code - Claude writes `a|b` in a code span without escaping it, and
// splitting there tore the row apart.
function mdCells(line) {
    let text = line.trim();
    if (text.startsWith('|')) {
        text = text.slice(1);
    }
    if (text.endsWith('|') && !text.endsWith('\\|')) {
        text = text.slice(0, -1);
    }
    const cells = [];
    let cell = '';
    let fence = 0;
    let index = 0;
    while (index < text.length) {
        const char = text[index];
        if (char === '\\' && text[index + 1] === '|') {
            cell += fence ? '\\|' : '|';
            index += 2;
            continue;
        }
        if (char === '`') {
            let run = 0;
            while (text[index + run] === '`') {
                run += 1;
            }
            // A run opens a code span only when a run as long closes it later.
            if (!fence && mdClosingRun(text, index + run, run)) {
                fence = run;
            } else if (run === fence) {
                fence = 0;
            }
            cell += text.slice(index, index + run);
            index += run;
            continue;
        }
        if (char === '|' && !fence) {
            cells.push(cell.trim());
            cell = '';
        } else {
            cell += char;
        }
        index += 1;
    }
    cells.push(cell.trim());
    return cells;
}

// Whether a run of exactly `run` backticks follows from `from` on.
function mdClosingRun(text, from, run) {
    let index = text.indexOf('`', from);
    while (index >= 0) {
        let length = 0;
        while (text[index + length] === '`') {
            length += 1;
        }
        if (length === run) {
            return true;
        }
        index = text.indexOf('`', index + length);
    }
    return false;
}

// Inline code and links first, on the raw text - neither is emphasized inside -
// then emphasis on the escaped rest.
function mdInline(raw) {
    const text = String(raw);
    let html = '';
    let last = 0;
    for (const match of text.matchAll(MD_INLINE)) {
        html += mdEmphasis(esc(text.slice(last, match.index)));
        if (match[1]) {
            html += '<code>' + esc(match[2].replace(/^ (.+) $/, '$1')) + '</code>';
        } else {
            html += '<span class="md-link"' + attr('data-tip', match[4]) + '>' + mdEmphasis(esc(match[3])) + '</span>';
        }
        last = match.index + match[0].length;
    }
    return html + mdEmphasis(esc(text.slice(last)));
}

// Emphasis on already escaped text: the markers are plain characters esc
// leaves alone, and every captured run is escaped text.
function mdEmphasis(escaped) {
    return escaped
        .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1<strong>$2</strong>')
        .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>')
        .replace(/(^|[^*\w])\*(?=[^\s*])([\s\S]*?[^\s*]|[^\s*])\*(?![*\w])/g, '$1<em>$2</em>')
        .replace(/(^|[^\w])_(?=[^\s_])([\s\S]*?[^\s_]|[^\s_])_(?!\w)/g, '$1<em>$2</em>');
}

/* --- the connection graph ---

   The live sessions, the subagents they started (grouped under their workflow
   run), their background processes and the messages between them. An edge with traffic in the last minute
   - a running subagent, a message just sent - carries moving dots. */

const GRAPH_ACTIVE_SECONDS = 60;
const GRAPH_NODE_W = 230;
const GRAPH_NODE_H = 38;
const GRAPH_COL_W = 274;
const GRAPH_ROW_H = 48;
const GRAPH_PAD = 16;
// Room left of the first column for the bend of a message between two sessions.
const GRAPH_LEFT = 52;
// The markup carries at most this many characters of a label; the renderer
// then shortens it to the room the box has (GRAPH_TEXT_WIDTH, in pixels).
const GRAPH_LABEL_MAX = 80;
const GRAPH_TEXT_X = 24;
// The right edge keeps room for a session's collapse toggle.
const GRAPH_TEXT_WIDTH = GRAPH_NODE_W - GRAPH_TEXT_X - 24;
// A subagent's id as Claude Code names its files: a message to or from one
// that is no longer in the recent window has nothing left to point at.
const AGENT_ID = /^a[0-9a-f]{12,24}$/;

// The subagents of the recent window, from the raw record, each with the model
// it mostly ran on and what it cost under `prices` (null without a rate).
function agentsOf(raw, prices) {
    const result = [];
    for (const agent of Array.isArray(raw.agents) ? raw.agents : []) {
        if (!agent || typeof agent.id !== 'string' || !agent.id) {
            continue;
        }
        const usage = agent.usage_by_model && typeof agent.usage_by_model === 'object' && !Array.isArray(agent.usage_by_model)
            ? agent.usage_by_model
            : {};
        result.push({
            skill: typeof agent.skill === 'string' ? agent.skill : '',
            model: mainModel(usage),
            cost_usd: sessionCostUsd(usage, prices),
            id: agent.id,
            type: typeof agent.type === 'string' ? agent.type : '',
            description: typeof agent.description === 'string' ? agent.description : '',
            running: agent.running === true,
            age_seconds: finiteOrNull(agent.age_seconds),
            parent_id: typeof agent.parent_id === 'string' ? agent.parent_id : '',
            workflow: typeof agent.workflow === 'string' ? agent.workflow : '',
            started_seconds: finiteOrNull(agent.started_seconds),
            tool_calls: countOrZero(agent.tool_calls),
            lines_added: countOrZero(agent.lines_added),
            lines_removed: countOrZero(agent.lines_removed),
            launch: AGENT_LAUNCHES.has(agent.launch) ? agent.launch : '',
            effort: typeof agent.effort === 'string' ? agent.effort : '',
            tools: toolsOf(agent.tools),
        });
    }
    return result;
}

// How a subagent can have been started; anything else on disk reads as unknown.
const AGENT_LAUNCHES = new Set(['foreground', 'background', 'skill']);

function countOrZero(value) {
    return Number.isInteger(value) && value > 0 ? value : 0;
}

// A subagent's title: its task description, the skill it ran when it has none
// (a forked skill writes no description), else its type.
function agentTitle(agent) {
    return agent.description || (agent.skill ? '/' + agent.skill : '') || agent.type || '';
}

// The agents in the order they started, earliest first, each with its number
// in that order. An agent whose start is still unknown (its file not scanned
// yet) sorts by its last activity; ties keep their id order, so the numbers
// stay put between polls.
function agentsByStart(agents) {
    const startOf = (agent) => (agent.started_seconds !== null ? agent.started_seconds : (agent.age_seconds !== null ? agent.age_seconds : 0));
    return agents.slice()
        .sort((left, right) => startOf(right) - startOf(left) || (left.id < right.id ? -1 : (left.id > right.id ? 1 : 0)))
        .map((agent, index) => Object.assign({}, agent, { number: index + 1 }));
}

// How long a subagent ran: from its start to its last write, or to now while it
// runs; null while its start is unknown.
function agentRuntime(agent) {
    if (!agent || agent.started_seconds === null || agent.started_seconds === undefined) {
        return null;
    }
    const end = agent.running || agent.age_seconds === null ? 0 : agent.age_seconds;
    return Math.max(0, agent.started_seconds - end);
}

// The tools a conversation called: each with how often and how long ago last.
function toolsOf(list) {
    const result = [];
    for (const tool of Array.isArray(list) ? list : []) {
        if (!tool || typeof tool.name !== 'string' || !tool.name || !Number.isInteger(tool.calls) || tool.calls <= 0) {
            continue;
        }
        result.push({ name: tool.name, calls: tool.calls, age_seconds: finiteOrNull(tool.age_seconds), failed: countOrZero(tool.failed) });
    }
    return result;
}

// The tools the graph draws under their caller. A subagent call, a message and
// a hand-back already are edges of their own, so they are not drawn twice.
const GRAPH_EDGE_TOOLS = new Set(['Agent', 'Task', 'SendMessage', 'SubagentHandback', 'StructuredOutput']);
// An MCP tool is named mcp__<server>__<tool>; the graph draws one node per server.
const MCP_TOOL = /^mcp__(.+?)__.+$/;

// The node a tool belongs to: its server for an MCP tool, else the tool itself.
function toolNodeKey(name) {
    const mcp = MCP_TOOL.exec(name);
    return mcp ? 'mcp:' + mcp[1] : name;
}

function toolNodeLabel(key) {
    return key.startsWith('mcp:') ? key.slice(4) : key;
}

// A subagent's steps as the details list them: tool calls with their input and
// result, and its replies between them, in order. Anything malformed is left out.
function agentStepsOf(steps) {
    const result = [];
    for (const step of Array.isArray(steps) ? steps : []) {
        if (!step || typeof step !== 'object') {
            continue;
        }
        if (step.kind === 'text' && typeof step.text === 'string' && step.text) {
            result.push({ kind: 'text', text: step.text, at: finiteOrNull(step.at) });
        } else if (step.kind === 'call' && typeof step.id === 'string' && step.id) {
            const input = step.input && typeof step.input === 'object' && !Array.isArray(step.input) ? step.input : {};
            result.push({
                kind: 'call', id: step.id, tool: typeof step.tool === 'string' ? step.tool : '', input: input,
                result: typeof step.result === 'string' ? step.result : null, error: step.error === true,
                outcome: STEP_OUTCOMES.has(step.outcome) ? step.outcome : '',
                exit_code: Number.isInteger(step.exit_code) ? step.exit_code : null,
                at: finiteOrNull(step.at), result_at: finiteOrNull(step.result_at),
            });
        }
    }
    return result;
}

// How a step can have ended besides normally: it failed, you rejected it, or it
// was stopped before it finished.
const STEP_OUTCOMES = new Set(['failed', 'rejected', 'interrupted']);

// What a tool call acts on, in one line: its description, command, path,
// pattern, address, query or prompt - else its first text input.
const STEP_SUBJECT_KEYS = ['description', 'command', 'file_path', 'notebook_path', 'pattern', 'url', 'query', 'prompt', 'path', 'skill'];

function stepSubject(step) {
    const input = step && step.input ? step.input : {};
    let subject = '';
    for (const key of STEP_SUBJECT_KEYS) {
        if (Object.prototype.hasOwnProperty.call(input, key) && typeof input[key] === 'string' && input[key].trim()) {
            subject = input[key];
            break;
        }
    }
    if (!subject) {
        subject = Object.values(input).find((value) => typeof value === 'string' && value.trim()) || '';
    }
    return firstLine(subject);
}

// When a step happened, from the first one: "+0:05", "+12:34", "+1:02:03".
function formatClock(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = String(total % 60).padStart(2, '0');
    return hours > 0 ? hours + ':' + String(minutes).padStart(2, '0') + ':' + secs : minutes + ':' + secs;
}

// How long a step took: tenths below ten seconds, whole seconds below a
// minute, then like an age.
function stepDuration(seconds, labels) {
    const value = Math.max(0, Number(seconds) || 0);
    if (value < 10) {
        return value.toFixed(1) + 's';
    }
    if (value < 60) {
        return Math.round(value) + 's';
    }
    return formatAge(value, labels || {});
}

// One input value as text: a string as written, anything else as indented JSON.
function stepInputText(value) {
    if (typeof value === 'string') {
        return value;
    }
    try {
        return JSON.stringify(value, null, 2);
    } catch (e) {
        return String(value);
    }
}

// The session's background processes as a tree: each with its pid, its name and
// the nearest listed process above it (0: the session itself).
function processesOf(raw) {
    const result = [];
    for (const item of Array.isArray(raw.child_processes) ? raw.child_processes : []) {
        if (!item || !Number.isInteger(item.pid) || item.pid <= 0 || typeof item.name !== 'string') {
            continue;
        }
        result.push({ pid: item.pid, parent: Number.isInteger(item.parent) && item.parent > 0 ? item.parent : 0, name: item.name });
    }
    return result;
}

// The model that wrote most of a usage-by-model map's output, or null.
function mainModel(usageByModel) {
    let best = null;
    let bestOutput = -1;
    for (const modelId of Object.keys(usageByModel)) {
        const usage = usageByModel[modelId];
        const output = usage && Number.isFinite(Number(usage.output_tokens)) ? Number(usage.output_tokens) : 0;
        if (modelId && output > bestOutput) {
            best = modelId;
            bestOutput = output;
        }
    }
    return best;
}

// The line under an agent node's title: its model, its cost and how long ago it
// was active. Everything else about it is in the details, on a click.
function agentFacts(node, labels) {
    return [formatModel(node.model), formatCost(node.cost_usd), formatAge(node.age_seconds, labels || {})].filter(Boolean);
}

// The messages of the last day, from the raw record.
function messagesOf(raw) {
    const result = [];
    for (const message of Array.isArray(raw.messages) ? raw.messages : []) {
        if (!message || (message.direction !== 'sent' && message.direction !== 'received')) {
            continue;
        }
        if (typeof message.peer !== 'string' || !message.peer) {
            continue;
        }
        result.push({
            direction: message.direction,
            peer: message.peer,
            label: typeof message.label === 'string' ? message.label : '',
            handback: message.handback === true,
            age_seconds: finiteOrNull(message.age_seconds),
        });
    }
    return result;
}

// How far back the graph reaches, "all" included. The backend lists every agent
// and message; the graph narrows them itself, so a switch costs no read.
const GRAPH_RANGES = HISTORY_RANGES;
const DEFAULT_GRAPH_RANGE = '24h';

// The range for a key, the default for an unknown one (a stale stored value).
function graphRange(key) {
    return GRAPH_RANGES.find((range) => range.key === key)
        || GRAPH_RANGES.find((range) => range.key === DEFAULT_GRAPH_RANGE);
}

// The agents inside the window: every running one, every one active within
// `maxAgeSeconds`, and the agents above them, so a subagent never loses the
// agent that started it. `null` keeps them all.
function agentsInWindow(agents, maxAgeSeconds) {
    if (maxAgeSeconds === null || maxAgeSeconds === undefined) {
        return agents;
    }
    const byId = new Map(agents.map((agent) => [agent.id, agent]));
    const keep = new Set();
    for (const agent of agents) {
        if (!agent.running && (agent.age_seconds === null || agent.age_seconds > maxAgeSeconds)) {
            continue;
        }
        let current = agent;
        while (current && !keep.has(current.id)) {
            keep.add(current.id);
            current = current.parent_id ? byId.get(current.parent_id) : null;
        }
    }
    return agents.filter((agent) => keep.has(agent.id));
}

// Whether a caller's tools (a session's, `agentId` null, or one of its
// subagents') are drawn: a selected subagent's own, a selected session's and
// those of all its subagents. Nothing selected, or a process, draws none.
function toolsInFocus(focus, key, agentId) {
    if (!focus || focus.process || focus.key !== key) {
        return false;
    }
    return !focus.agent || focus.agent === agentId;
}

// Nodes and edges from `entries` ([{key, session}], the built sessions to show),
// limited to the agents and messages of the last `maxAgeSeconds` (null: all);
// `focus` ({key, agent, process, tool}) decides whose tools are drawn, and a
// session whose id is in `collapsed` (a Set) keeps its subagents, processes and
// tools folded away - its node says how many - while its messages stay.
function graphModel(entries, maxAgeSeconds = null, focus = null, collapsed = null) {
    const inWindow = (ageSeconds) => maxAgeSeconds === null || (ageSeconds !== null && ageSeconds <= maxAgeSeconds);
    const nodes = [];
    const byId = new Map();
    const edges = new Map();
    const addNode = (node) => {
        if (!byId.has(node.id)) {
            byId.set(node.id, node);
            nodes.push(node);
        }
        return byId.get(node.id);
    };
    const addEdge = (from, to, kind, ageSeconds, active, count = 1) => {
        const id = from + '>' + to + '>' + kind;
        const edge = edges.get(id) || { from: from, to: to, kind: kind, count: 0, active: false, age_seconds: null };
        edge.count += count;
        edge.active = edge.active || Boolean(active);
        if (ageSeconds !== null && (edge.age_seconds === null || ageSeconds < edge.age_seconds)) {
            edge.age_seconds = ageSeconds;
        }
        edges.set(id, edge);
    };
    // The tools a session or a subagent called within the window, each a node of
    // its own under that caller (one per MCP server), carrying the calls - drawn
    // only for what is in focus, since every caller's tools at once would bury
    // the graph.
    const addToolNodes = (actorNode, tools, key, agentId, root) => {
        if (!toolsInFocus(focus, key, agentId)) {
            return;
        }
        for (const tool of tools || []) {
            if (GRAPH_EDGE_TOOLS.has(tool.name) || !inWindow(tool.age_seconds)) {
                continue;
            }
            const toolKey = toolNodeKey(tool.name);
            const node = addNode({
                id: actorNode + '/t:' + toolKey, kind: 'tool', key: key, agent: agentId, tool: toolKey, label: toolNodeLabel(toolKey),
                mcp: toolKey.startsWith('mcp:'), calls: 0, failed: 0, parent: actorNode, root: root,
            });
            node.calls += tool.calls;
            node.failed += tool.failed || 0;
            const age = tool.age_seconds;
            addEdge(actorNode, node.id, 'tool', age, age !== null && age < GRAPH_ACTIVE_SECONDS, tool.calls);
        }
    };

    // A message names its peer by session id, by name or by agent id.
    const sessionByName = new Map();
    const sessionIds = [];
    for (const { key, session } of entries) {
        const id = 's:' + key;
        addNode({ id: id, kind: 'session', key: key, label: session.name || '', status: session.status, parent: null, root: id });
        if (typeof session.session_id === 'string' && session.session_id) {
            sessionIds.push([session.session_id.toLowerCase(), id]);
        }
        for (const name of [session.name, session.short_name, session.title]) {
            if (typeof name === 'string' && name && !sessionByName.has(name.toLowerCase())) {
                sessionByName.set(name.toLowerCase(), id);
            }
        }
    }
    const findSession = (peer) => {
        const lower = peer.toLowerCase();
        if (sessionByName.has(lower)) {
            return sessionByName.get(lower);
        }
        const match = lower.length >= 8 ? sessionIds.find(([sessionId]) => sessionId.startsWith(lower)) : null;
        return match ? match[1] : null;
    };

    for (const { key, session } of entries) {
        const sessionNode = 's:' + key;
        // Numbered over every listed agent, so a number stays the same whatever
        // the window shows; drawn top to bottom in the order they started.
        const listedAgents = agentsInWindow(agentsByStart(session.agents || []), maxAgeSeconds);
        const processes = session.process_list || [];
        const below = listedAgents.length + (processes.length || session.processes || 0);
        const folded = Boolean(collapsed && session.session_id && collapsed.has(session.session_id));
        Object.assign(byId.get(sessionNode), { sessionId: session.session_id || '', below: below, collapsed: folded });
        const agents = folded ? [] : listedAgents;
        const agentNodes = new Map(agents.map((agent) => [agent.id, sessionNode + '/a:' + agent.id]));
        for (const agent of agents) {
            let parent = sessionNode;
            if (agent.workflow) {
                const workflowNode = sessionNode + '/w:' + agent.workflow;
                if (!byId.has(workflowNode)) {
                    addNode({ id: workflowNode, kind: 'workflow', label: '', parent: sessionNode, root: sessionNode, running: false });
                    addEdge(sessionNode, workflowNode, 'spawn', null, false);
                }
                parent = workflowNode;
                if (agent.running) {
                    byId.get(workflowNode).running = true;
                }
            }
            if (agent.parent_id && agentNodes.has(agent.parent_id) && agent.parent_id !== agent.id) {
                parent = agentNodes.get(agent.parent_id);
            }
            addNode({
                id: agentNodes.get(agent.id), kind: 'agent', key: key, agent: agent.id, number: agent.number, label: agentTitle(agent),
                model: agent.model, cost_usd: agent.cost_usd, age_seconds: agent.age_seconds,
                running: agent.running, parent: parent, root: sessionNode,
            });
            addEdge(parent, agentNodes.get(agent.id), 'spawn', agent.age_seconds, agent.running);
            addToolNodes(agentNodes.get(agent.id), agent.tools, key, agent.id, sessionNode);
        }

        // The background processes the session's tools started, each under the
        // process that started it; only a count when the list is unknown.
        const listed = new Set(processes.map((process) => process.pid));
        for (const process of folded ? [] : processes) {
            const processNode = sessionNode + '/p:' + process.pid;
            const parent = process.parent && listed.has(process.parent) && process.parent !== process.pid ? sessionNode + '/p:' + process.parent : sessionNode;
            addNode({
                id: processNode, kind: 'process', key: key, process: process.pid, label: process.name,
                parent: parent, root: sessionNode, running: true,
            });
            addEdge(parent, processNode, 'spawn', 0, true);
        }
        if (!folded) {
            addToolNodes(sessionNode, session.tools, key, null, sessionNode);
        }
        if (!folded && processes.length === 0 && session.processes > 0) {
            const processNode = sessionNode + '/p';
            addNode({ id: processNode, kind: 'process', count: session.processes, parent: sessionNode, root: sessionNode, running: true });
            addEdge(sessionNode, processNode, 'spawn', 0, true);
        }

        for (const message of session.messages || []) {
            if (!inWindow(message.age_seconds)) {
                continue;
            }
            let other = agentNodes.get(message.peer) || findSession(message.peer);
            if (!other && (message.handback || AGENT_ID.test(message.peer))) {
                continue;
            }
            if (!other) {
                other = 'p:' + message.peer.toLowerCase();
                addNode({ id: other, kind: 'peer', label: message.label || message.peer, parent: null, root: null });
            }
            if (other === sessionNode) {
                continue;
            }
            const sent = message.direction === 'sent';
            const age = message.age_seconds;
            addEdge(sent ? sessionNode : other, sent ? other : sessionNode, 'message', age, age !== null && age < GRAPH_ACTIVE_SECONDS);
        }
    }
    for (const edge of edges.values()) {
        if (edge.kind === 'spawn' && byId.get(edge.to) && byId.get(edge.to).kind === 'workflow') {
            edge.active = byId.get(edge.to).running;
        }
    }
    return { nodes: nodes, edges: Array.from(edges.values()) };
}

// Positions for the model: each session's tree runs left to right, one row per
// leaf, sessions stacked; peers outside every tree sit in a column of their own.
function graphLayout(model) {
    const children = new Map();
    for (const node of model.nodes) {
        if (node.parent) {
            if (!children.has(node.parent)) {
                children.set(node.parent, []);
            }
            children.get(node.parent).push(node);
        }
    }
    const positions = new Map();
    let row = 0;
    let maxDepth = 0;
    const place = (node, depth, seen) => {
        if (seen.has(node.id)) {
            return row;
        }
        seen.add(node.id);
        maxDepth = Math.max(maxDepth, depth);
        const kids = children.get(node.id) || [];
        let y;
        if (kids.length === 0) {
            y = row;
            row += 1;
        } else {
            const rows = kids.map((kid) => place(kid, depth + 1, seen));
            y = (rows[0] + rows[rows.length - 1]) / 2;
        }
        positions.set(node.id, { depth: depth, row: y });
        return y;
    };
    const seen = new Set();
    for (const node of model.nodes) {
        if (node.kind === 'session') {
            place(node, 0, seen);
            row += 0.35;
        }
    }

    // Peers line up beside the first node they talk to, never overlapping.
    const peers = model.nodes.filter((node) => node.kind === 'peer');
    let nextPeerRow = 0;
    for (const peer of peers) {
        const edge = model.edges.find((item) => (item.from === peer.id && positions.has(item.to)) || (item.to === peer.id && positions.has(item.from)));
        const partner = edge ? positions.get(edge.from === peer.id ? edge.to : edge.from) : null;
        const wanted = partner ? partner.row : nextPeerRow;
        const at = Math.max(wanted, nextPeerRow);
        positions.set(peer.id, { depth: maxDepth + 1, row: at });
        nextPeerRow = at + 1;
    }

    const placed = [];
    let width = 0;
    let height = 0;
    for (const node of model.nodes) {
        const position = positions.get(node.id);
        if (!position) {
            continue;
        }
        const x = GRAPH_LEFT + position.depth * GRAPH_COL_W;
        const y = GRAPH_PAD + position.row * GRAPH_ROW_H;
        placed.push(Object.assign({}, node, { x: x, y: y }));
        width = Math.max(width, x + GRAPH_NODE_W + GRAPH_PAD);
        height = Math.max(height, y + GRAPH_NODE_H + GRAPH_PAD);
    }
    const at = new Map(placed.map((node) => [node.id, node]));
    const lines = [];
    for (const edge of model.edges) {
        const from = at.get(edge.from);
        const to = at.get(edge.to);
        if (!from || !to) {
            continue;
        }
        lines.push(Object.assign({}, edge, { path: graphEdgePath(from, to) }));
    }
    return { nodes: placed, edges: lines, width: width, height: height };
}

// A curve from the right of `from` to the left of `to`; between two nodes of
// one column (two sessions) it bends out to the left.
function graphEdgePath(from, to) {
    const midFrom = from.y + GRAPH_NODE_H / 2;
    const midTo = to.y + GRAPH_NODE_H / 2;
    const round = (value) => Math.round(value * 10) / 10;
    if (to.x > from.x) {
        const x1 = from.x + GRAPH_NODE_W;
        const x2 = to.x;
        const bend = (x2 - x1) / 2;
        return 'M' + round(x1) + ' ' + round(midFrom) + ' C' + round(x1 + bend) + ' ' + round(midFrom) + ' '
            + round(x2 - bend) + ' ' + round(midTo) + ' ' + round(x2) + ' ' + round(midTo);
    }
    if (to.x < from.x) {
        return graphEdgePath(to, from);
    }
    const x = from.x;
    const out = GRAPH_LEFT - 8;
    return 'M' + round(x) + ' ' + round(midFrom) + ' C' + round(x - out) + ' ' + round(midFrom) + ' '
        + round(x - out) + ' ' + round(midTo) + ' ' + round(x) + ' ' + round(midTo);
}

// A session's fold toggle at the right edge of its box: the whole height is the
// hit area, the glyph says which way a click goes.
function graphToggleMarkup(node) {
    return '<g class="gnode-toggle"' + attr('data-toggle', node.sessionId) + attr('transform', 'translate(' + (GRAPH_NODE_W - 22) + ' 0)') + '>'
        + '<rect' + attr('width', 22) + attr('height', GRAPH_NODE_H) + '></rect>'
        + '<text' + attr('x', 11) + attr('y', GRAPH_NODE_H / 2 + 4) + ' text-anchor="middle">' + (node.collapsed ? '▸' : '▾') + '</text></g>';
}

function clipLabel(text, max) {
    const value = typeof text === 'string' ? text : '';
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

// The graph as SVG markup. Every text and attribute value passes through
// esc/attr; the animated dots follow their edge by reference to its path id.
// `selection` is the selected session's key, or {key, agent, process} when one
// of its subagents or processes is selected; such a node carries its session's
// key beside its own id, so a click can select it.
function graphMarkup(layout, labels, selection) {
    const strings = labels || {};
    const selected = selection && typeof selection === 'object' ? selection : { key: selection, agent: null };
    const width = Math.max(layout.width, 1);
    const height = Math.max(layout.height, 1);
    let edgesHtml = '';
    let dotsHtml = '';
    layout.edges.forEach((edge, index) => {
        const id = 'graph-edge-' + index;
        let tip = '';
        if (edge.kind === 'message') {
            tip = fmt(strings.graph_messages, { count: edge.count });
        } else if (edge.kind === 'tool') {
            tip = fmt(strings.graph_tool_calls, { count: edge.count });
        }
        edgesHtml += '<path' + attr('id', id) + attr('class', 'edge edge-' + edge.kind + (edge.active ? ' active' : ''))
            + attr('d', edge.path) + (tip ? attr('data-tip', tip) : '') + '></path>';
        if (edge.active) {
            for (const delay of [0, 0.9]) {
                dotsHtml += '<circle' + attr('class', 'dot dot-' + edge.kind) + ' r="3">'
                    + '<animateMotion' + attr('dur', '1.8s') + attr('begin', '-' + delay + 's') + ' repeatCount="indefinite">'
                    + '<mpath' + attr('href', '#' + id) + '></mpath></animateMotion></circle>';
            }
        }
    });

    let nodesHtml = '';
    for (const node of layout.nodes) {
        let label = node.label;
        if (node.kind === 'agent') {
            label = '#' + node.number + ' ' + node.label;
        } else if (node.kind === 'workflow') {
            label = strings.graph_workflow || '';
        } else if (node.kind === 'process' && !node.process) {
            label = fmt(strings.signal_processes, { count: node.count });
        }
        let sub = '';
        if (node.kind === 'agent') {
            sub = agentFacts(node, strings).join(' · ');
        } else if (node.kind === 'process' && node.process) {
            sub = (strings.fact_pid || 'PID') + ' ' + node.process;
        } else if (node.kind === 'session' && node.collapsed && node.below > 0) {
            sub = fmt(strings.graph_hidden, { count: node.below });
        } else if (node.kind === 'peer') {
            sub = strings.graph_peer || '';
        } else if (node.kind === 'tool') {
            sub = (node.mcp ? (strings.graph_mcp || '') + ' · ' : '') + fmt(strings.graph_tool_calls, { count: node.calls })
                + (node.failed > 0 ? ' · ' + fmt(strings.graph_tool_failed, { count: node.failed }) : '');
        }
        // The tooltip only completes a title the box cut short.
        const tip = node.kind === 'workflow' ? '' : label;
        // A node is selected when everything it names matches the selection: its
        // session, and its subagent, process or tool, or none of them.
        const isSelected = Boolean(node.key) && node.key === selected.key && (node.agent || null) === (selected.agent || null)
            && (node.process || null) === (selected.process || null) && (node.tool || null) === (selected.tool || null);
        const classes = 'gnode gnode-' + node.kind
            + (node.kind === 'session' ? ' status-' + node.status : '')
            + (node.running ? ' running' : '')
            + (node.failed > 0 ? ' failed' : '')
            + (isSelected ? ' selected' : '');
        nodesHtml += '<g' + attr('class', classes) + attr('transform', 'translate(' + node.x + ' ' + node.y + ')')
            + (node.key ? attr('data-key', node.key) : '') + (node.agent ? attr('data-agent', node.agent) : '')
            + (node.process ? attr('data-process', node.process) : '')
            + (node.kind === 'tool' ? attr('data-tool', node.tool) : '')
            + (tip ? attr('data-tip', tip) : '') + '>'
            + '<rect' + attr('width', GRAPH_NODE_W) + attr('height', GRAPH_NODE_H) + ' rx="7"></rect>'
            + '<circle class="gnode-dot" cx="13"' + attr('cy', GRAPH_NODE_H / 2) + ' r="4"></circle>'
            + '<text class="gnode-label"' + attr('x', GRAPH_TEXT_X) + attr('y', sub ? 16 : GRAPH_NODE_H / 2 + 4) + '>' + esc(clipLabel(label, GRAPH_LABEL_MAX)) + '</text>'
            + (sub ? '<text class="gnode-sub"' + attr('x', GRAPH_TEXT_X) + ' y="29">' + esc(clipLabel(sub, GRAPH_LABEL_MAX)) + '</text>' : '')
            + (node.kind === 'session' && node.below > 0 && node.sessionId ? graphToggleMarkup(node) : '')
            + '</g>';
    }
    return '<svg class="graph"' + attr('viewBox', '0 0 ' + width + ' ' + height) + attr('width', width) + attr('height', height)
        + '>'
        + '<g class="edges">' + edgesHtml + '</g><g class="dots">' + dotsHtml + '</g><g class="nodes">' + nodesHtml + '</g></svg>';
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
    const newestVersions = newestCliVersions(rawSessions);
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
        const session = buildSession(raw, labels, prices);
        // Needs every session at once, which buildSession never sees.
        session.cli_update = cliUpdateFor(raw, newestVersions);
        group.sessions.push(session);
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
    LIST_SECTIONS,
    LIST_SIGNAL_LIMIT,
    sectionOf,
    sectionActive,
    toggledSectionKeys,
    sortSectionSessions,
    sortGroupSessions,
    buildSections,
    flattenProjects,
    sessionSignals,
    searchRegExp,
    sessionMetaMatch,
    attentionUpdate,
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
    modelRate,
    CACHE_REWRITE_BADGE_USD,
    cacheOutlook,
    cacheLine,
    sessionTotalCostUsd,
    webSearchPrice,
    costTipLines,
    costShares,
    formatCost,
    usageTotalTokens,
    modelHistory,
    cliHistory,
    changelogUrl,
    parseVersion,
    compareVersions,
    newestCliVersions,
    cliUpdateFor,
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
    artifactsOf,
    firstLine,
    SUBAGENT_TOOLS,
    toolDisplayName,
    currentToolOf,
    toolCallLine,
    activityOf,
    planItemsOf,
    linesOf,
    WORK_WINDOW_SECONDS,
    workSummary,
    testsLabel,
    agentsOf,
    finiteOrNull,
    processesOf,
    toolsOf,
    toolNodeKey,
    toolNodeLabel,
    toolsInFocus,
    agentStepsOf,
    stepSubject,
    stepInputText,
    formatClock,
    stepDuration,
    agentFacts,
    markdownToHtml,
    agentTitle,
    agentsByStart,
    agentRuntime,
    mainModel,
    messagesOf,
    GRAPH_ACTIVE_SECONDS,
    GRAPH_RANGES,
    DEFAULT_GRAPH_RANGE,
    graphRange,
    agentsInWindow,
    GRAPH_TEXT_WIDTH,
    graphModel,
    graphLayout,
    graphEdgePath,
    graphMarkup,
    panelBranch,
    buildSession,
    groupProjects,
    projectBand,
    projectActivityAge,
    sortProjects,
    STATUS_ORDER,
    STATUS_BAND,
    STATUS_FILTER,
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = AMC_LOGIC;
}
if (typeof window !== 'undefined') {
    window.AMC_LOGIC = AMC_LOGIC;
}
