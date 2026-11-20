'use strict';

/* --- filtering: the list sections and the grouping --- */

// A session shows while its status bucket is active. The buckets are opened and
// closed in groups, one group per list section (logic.LIST_SECTIONS).
function matchesFilter(session) {
    const bucket = logic.sessionBucket(session, state.desktopGroups.archived);
    return bucket != null && state.filters.has(bucket);
}

// Whether a section that lists past sessions is open: "Older", or "Archived",
// which holds the past sessions Claude Desktop archived. One fetch feeds both.
function historyWanted() {
    return state.filters.has('history') || state.filters.has('archived');
}

// Open or close a section, i.e. switch all its buckets on or off.
function toggleSection(key) {
    const wasHistory = historyWanted();
    state.filters = logic.toggledSectionKeys(key, state.filters);
    persistFilters();

    // Opening "Older" or "Archived" triggers the (potentially second-long) scan
    // of past sessions - lazily, off the UI thread. When it arrives,
    // afterHistoryLoaded re-runs any active search over the wider scope and
    // re-renders.
    if (!wasHistory && historyWanted() && (state.history === null || state.historyStale)) {
        ensureHistoryLoaded();
        return;
    }

    // An open section is part of the search scope, so re-run an active search.
    if (state.search.trim().length >= SEARCH_MIN_CHARS) {
        runSearch();
    } else if (state.last) {
        render(state.last);
    }
}

/* --- Pinned and Claude Desktop's groups --- */

// The pins and Desktop groups the list is split by (logic.buildSections), with
// each section's folding: the user's own where there is one, else Desktop's.
function currentGroupsView() {
    const collapsed = new Set();
    if (state.groupCollapsed.get('pinned') === true) {
        collapsed.add('pinned');
    }
    for (const group of state.desktopGroups.groups) {
        const key = 'group:' + group.id;
        const own = state.groupCollapsed.get(key);
        if (own === undefined ? group.collapsed : own) {
            collapsed.add(key);
        }
    }
    return {
        pinned: new Set(state.pins.keys()),
        groups: state.desktopGroups.groups,
        members: state.desktopGroups.members,
        archived: state.desktopGroups.archived,
        collapsed: collapsed,
    };
}

// The groups view the list's sections are built with: Pinned and the Desktop
// groups are sections of the urgency grouping only (the compact view included);
// elsewhere every session counts by its status, an archived one that ended under
// "Archived". The search scope uses the same view, so it is the visible set.
function listGroupsView() {
    const view = currentGroupsView();
    const grouping = state.compact ? 'urgency' : state.group;
    return grouping === 'urgency' ? view : { archived: view.archived };
}

// Read the pins back (amc-pins); anything unreadable is no pins.
function loadPins() {
    let stored = null;
    try {
        stored = localStorage.getItem('amc-pins');
    } catch (e) { /* storage unavailable */ }
    state.pins = logic.parsePins(stored || '[]');
    state.pinMisses = new Map();
}

function savePins() {
    try {
        localStorage.setItem('amc-pins', JSON.stringify(Array.from(state.pins, ([id, origin]) => ({ id: id, origin: origin }))));
    } catch (e) { /* storage unavailable */ }
}

// Pin the session to the top of the list, or take the pin off. A kept ended
// session has to appear or go, so the snapshot is asked for again.
function togglePin(session) {
    const id = session && session.session_id;
    if (!logic.isPinId(id)) {
        return;
    }
    if (state.pins.has(id)) {
        state.pins.delete(id);
    } else if (state.pins.size < logic.MAX_PINS) {
        state.pins.set(id, session.origin || 'local');
    } else {
        toast(logic.fmt(state.labels.pins_full, { max: logic.MAX_PINS }));
        return;
    }
    state.pinMisses.delete(id);
    savePins();
    if (state.last) {
        render(state.last);
    }
    tick();
}

// A session deleted from History is gone for good; so is its pin.
function unpinSession(sessionId) {
    if (!state.pins.delete(sessionId)) {
        return;
    }
    state.pinMisses.delete(sessionId);
    savePins();
}

// Fold or unfold one group section, and remember it.
const GROUP_COLLAPSED_MAX = 100;

function toggleGroupSection(key) {
    if (!key) {
        return;
    }
    const folded = currentGroupsView().collapsed.has(key);
    // Re-inserted, so the most recent choices are the ones the cap keeps.
    state.groupCollapsed.delete(key);
    state.groupCollapsed.set(key, !folded);
    const entries = Array.from(state.groupCollapsed.entries()).slice(-GROUP_COLLAPSED_MAX);
    state.groupCollapsed = new Map(entries);
    try {
        localStorage.setItem('amc-group-collapsed', JSON.stringify(entries));
    } catch (e) { /* storage unavailable */ }
    if (state.last) {
        render(state.last);
    }
}

// Read the remembered folding back: pairs of a section key and a boolean, any
// other shape skipped.
function loadGroupCollapsed() {
    state.groupCollapsed = new Map();
    try {
        const stored = JSON.parse(localStorage.getItem('amc-group-collapsed') || '[]');
        for (const entry of Array.isArray(stored) ? stored.slice(-GROUP_COLLAPSED_MAX) : []) {
            if (Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1] === 'boolean') {
                state.groupCollapsed.set(entry[0], entry[1]);
            }
        }
    } catch (e) {
        state.groupCollapsed = new Map();
    }
}

const GROUPS = ['urgency', 'project', 'graph'];

function initListControls() {
    try {
        const stored = localStorage.getItem('amc-group');
        state.group = GROUPS.includes(stored) ? stored : 'urgency';
        state.graphRange = logic.graphRange(localStorage.getItem('amc-graph-range')).key;
        const folded = JSON.parse(localStorage.getItem('amc-graph-collapsed') || '[]');
        state.graphCollapsed = new Set(Array.isArray(folded) ? folded.filter((id) => typeof id === 'string') : []);
        state.graphLegend = localStorage.getItem('amc-graph-legend') !== '0';
    } catch (e) {
        state.group = 'urgency';
        state.graphRange = logic.DEFAULT_GRAPH_RANGE;
        state.graphCollapsed = new Set();
        state.graphLegend = true;
    }
    document.querySelectorAll('#group-switch [data-group]').forEach((button) => {
        button.addEventListener('click', () => setGroup(button.dataset.group));
    });
    refreshGroupSwitch();
}

function setGroup(group) {
    const wasGraph = state.group === 'graph';
    state.group = GROUPS.includes(group) ? group : 'urgency';
    try {
        localStorage.setItem('amc-group', state.group);
    } catch (e) { /* storage unavailable */ }
    refreshGroupSwitch();
    if (state.last) {
        render(state.last);
    }
    // The graph's subagents come with the snapshot only while it is shown.
    if (!wasGraph && state.group === 'graph') {
        tick();
    }
}

// How far back the graph reaches. Everything it needs is on the snapshot
// already, so a switch only re-renders.
function setGraphRange(key) {
    state.graphRange = logic.graphRange(key).key;
    try {
        localStorage.setItem('amc-graph-range', state.graphRange);
    } catch (e) { /* storage unavailable */ }
    if (state.last) {
        render(state.last);
    }
}

// Show or hide the graph's legend.
function toggleGraphLegend() {
    state.graphLegend = !state.graphLegend;
    try {
        localStorage.setItem('amc-graph-legend', state.graphLegend ? '1' : '0');
    } catch (e) { /* storage unavailable */ }
    if (state.last) {
        render(state.last);
    }
}

// Fold or unfold one session's subtree in the graph, or all of them at once.
// Remembered by session id, so a session reopened in a new process stays as it was.
const GRAPH_COLLAPSED_MAX = 500;

function toggleGraphSession(sessionId) {
    if (state.graphCollapsed.has(sessionId)) {
        state.graphCollapsed.delete(sessionId);
    } else {
        state.graphCollapsed.add(sessionId);
    }
    persistGraphCollapsed();
}

function setGraphCollapsed(all) {
    state.graphCollapsed = new Set();
    if (all) {
        for (const session of state.sessionsByKey.values()) {
            if (!session.is_history && session.session_id) {
                state.graphCollapsed.add(session.session_id);
            }
        }
    }
    persistGraphCollapsed();
}

function persistGraphCollapsed() {
    const ids = Array.from(state.graphCollapsed).slice(-GRAPH_COLLAPSED_MAX);
    state.graphCollapsed = new Set(ids);
    try {
        localStorage.setItem('amc-graph-collapsed', JSON.stringify(ids));
    } catch (e) { /* storage unavailable */ }
    if (state.last) {
        render(state.last);
    }
}

// The switch's labels and checked state; called on init and once labels arrive.
function refreshGroupSwitch() {
    const labels = state.labels;
    const switchEl = document.getElementById('group-switch');
    if (!switchEl) {
        return;
    }
    switchEl.setAttribute('aria-label', labels.group_by || '');
    switchEl.querySelectorAll('[data-group]').forEach((button) => {
        button.textContent = labels['group_' + button.dataset.group] || button.dataset.group;
        button.setAttribute('aria-checked', button.dataset.group === state.group ? 'true' : 'false');
    });
}

// The cached past sessions narrowed to the selected window. The cache can hold
// a wider fetch than the current selection (picking a narrower window never
// re-scans), so every reader of history goes through here rather than touching
// state.history directly - the raw cache is only for the fetch and for dropping
// a deleted session.
function visibleHistory() {
    return logic.filterHistoryByAge(state.history, logic.historyRangeSeconds(state.historyRange));
}

// Switch how far back the history listing reaches. A narrower window is served
// from the cache; only a wider one needs the backend again. Picking a window
// also opens "Older" - the range buttons sit in its open header, but the search's
// widening offer calls this too, and passes activateChip false: it asked to look
// further back, not to list every past session, and opening the section would
// leave those rows behind once the query is cleared.
function setHistoryRange(key, activateChip = true) {
    const range = logic.historyRange(key);
    const needsFetch = !logic.historyRangeCovered(state.historyLoadedSeconds, range.seconds);

    state.historyRange = range.key;
    try {
        localStorage.setItem('amc-history-range', range.key);
    } catch (e) { /* storage unavailable */ }

    const wasActive = state.filters.has('history');
    if (activateChip && !wasActive) {
        state.filters.add('history');
        persistFilters();
    }

    if (needsFetch) {
        // The cached rows stay until the wider ones land; a fetch already
        // running for the narrower window is followed by one for this one.
        state.historyStale = true;
        ensureHistoryLoaded();
        return;
    }

    // The visible set changed, so an active search has to re-run over the new
    // scope - exactly as opening a section does.
    if (state.search.trim().length >= SEARCH_MIN_CHARS) {
        runSearch();
    } else if (state.last) {
        render(state.last);
    }
}

// Fetch past sessions once, the first time "Older" is opened, and
// cache them. The bridge call runs on a pywebview worker thread, so awaiting it
// does not block the WebView: the live overview stays interactive and a loading
// note shows until the scan returns.
async function ensureHistoryLoaded() {
    if ((state.history !== null && !state.historyStale) || state.historyLoading) {
        // Already loading (or loaded): still reflect the current section and
        // loading state. Opening "Older" again during the in-flight first load
        // lands here, and without this render the section would look closed with
        // no loading note until the fetch lands or the next poll.
        if (state.last) {
            render(state.last);
        }
        return;
    }

    const bridge = apiBridge();
    if (!bridge || typeof bridge.get_history !== 'function') {
        // Browser preview: use the fabricated history if dev-mock provided any.
        // Without a bridge and not in mock mode the data source is not ready yet;
        // leave history null so a later call (a re-toggle, or boot) retries.
        if (mockMode()) {
            // The fabricated history is the whole (tiny) set, so it covers every
            // window; the selected range then narrows it client-side like a real
            // wider fetch would.
            state.history = Array.isArray(window.__MOCK_HISTORY__) ? window.__MOCK_HISTORY__ : [];
            state.historyStale = false;
            state.historyLoadedSeconds = null;
            state.historyReceivedAt = Date.now();
            afterHistoryLoaded();
        }
        return;
    }

    state.historyStale = false;
    state.historyLoading = true;
    if (state.last) {
        render(state.last);
    }

    // The window is read once, up front: the selection can change while the
    // scan is in flight, and the cache must be stamped with the window the
    // records actually came from, never with a newer one it does not cover.
    // The live sessions are read with it: the scan leaves out every session
    // still live at that moment, so one that ends while it runs is in neither.
    const windowSeconds = logic.historyRangeSeconds(state.historyRange);
    const basis = state.last ? state.last.sessions : null;
    try {
        const history = await bridge.get_history(windowSeconds);
        state.history = Array.isArray(history) ? history : [];
        state.historyLoadedSeconds = windowSeconds;
        state.historyReceivedAt = Date.now();
    } catch (e) {
        // A failed fetch must not cache []: that empty sentinel is what a
        // genuine empty result uses, so the loaded-guard would be satisfied
        // forever and never retry. Rows from an earlier fetch stay; without
        // any, null lets toggling History off and on (or the next
        // invalidation) fetch again.
        if (!Array.isArray(state.history)) {
            state.history = null;
            state.historyLoadedSeconds = undefined;
        }
    }

    state.historyLoading = false;
    if (!logic.historyRangeCovered(windowSeconds, logic.historyRangeSeconds(state.historyRange))
        || (basis && state.last && logic.historyNeedsRefresh(basis, state.last.sessions))) {
        state.historyStale = true;
    }
    if (state.historyStale) {
        ensureHistoryLoaded();
        return;
    }
    afterHistoryLoaded();
}

// Fetch the past sessions again, keeping the cached rows on screen until the
// new ones land - blanking them first would drop every past session from the
// list for the length of the scan, and a selected one with them. Run after the
// current render, which may be the caller: the fetch renders as it starts.
function refreshHistory() {
    state.historyStale = true;
    setTimeout(ensureHistoryLoaded, 0);
}

// Past sessions just became part of the view. Re-run any active search so its
// scope now covers them too (they were not in the scope when it first ran).
function afterHistoryLoaded() {
    if (state.search.trim().length >= SEARCH_MIN_CHARS) {
        runSearch();
    } else if (state.last) {
        render(state.last);
    }
}

function persistFilters() {
    try {
        localStorage.setItem('amc-filter', JSON.stringify([...state.filters]));
    } catch (e) { /* storage unavailable */ }
}

// Restore the persisted selection as a set of active bucket keys. Every section
// but "Older" is open by default; a stored selection restores exactly which
// buckets the user left on. Tolerates the legacy single-value format ("all" meant show-everything;
// any other value was one exclusive bucket) and drops keys no longer valid.
function loadFilters() {
    let stored;
    try {
        stored = localStorage.getItem('amc-filter');
    } catch (e) {
        return new Set(DEFAULT_FILTER_KEYS);
    }
    if (stored == null) {
        return new Set(DEFAULT_FILTER_KEYS);
    }

    let values = null;
    try {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) {
            values = parsed;
        }
    } catch (e) { /* not the array format - fall through to the legacy value */ }
    if (values == null) {
        values = stored === 'all' ? [...DEFAULT_FILTER_KEYS] : [stored];
    }

    const active = new Set(values.filter((key) => FILTER_KEYS.has(key)));

    // An empty selection would hide every session - never a useful state to
    // restore into (and how a legacy "show everything" was stored), so fall
    // back to the default sections open.
    return active.size > 0 ? active : new Set(DEFAULT_FILTER_KEYS);
}
