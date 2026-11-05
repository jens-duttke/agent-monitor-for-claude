'use strict';

/* --- filtering: the list sections and the grouping --- */

// A session shows while its status bucket is active. The buckets are opened and
// closed in groups, one group per list section (logic.LIST_SECTIONS).
function matchesFilter(session) {
    const bucket = logic.sessionBucket(session);
    return bucket != null && state.filters.has(bucket);
}

// Open or close a section, i.e. switch all its buckets on or off.
function toggleSection(key) {
    const wasHistory = state.filters.has('history');
    state.filters = logic.toggledSectionKeys(key, state.filters);
    persistFilters();

    // Opening "Older" triggers the (potentially second-long) scan of past
    // sessions - lazily, off the UI thread. When it arrives, afterHistoryLoaded
    // re-runs any active search over the wider scope and re-renders.
    if (!wasHistory && state.filters.has('history') && state.history === null) {
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

const GROUPS = ['urgency', 'project', 'graph'];

function initListControls() {
    try {
        const stored = localStorage.getItem('amc-group');
        state.group = GROUPS.includes(stored) ? stored : 'urgency';
        state.graphRange = logic.graphRange(localStorage.getItem('amc-graph-range')).key;
    } catch (e) {
        state.group = 'urgency';
        state.graphRange = logic.DEFAULT_GRAPH_RANGE;
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
        state.history = null;
        state.historyLoadedSeconds = undefined;
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
    if (state.history !== null || state.historyLoading) {
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
            state.historyLoadedSeconds = null;
            state.historyReceivedAt = Date.now();
            afterHistoryLoaded();
        }
        return;
    }

    state.historyLoading = true;
    if (state.last) {
        render(state.last);
    }

    // The window is read once, up front: the selection can change while the
    // scan is in flight, and the cache must be stamped with the window the
    // records actually came from, never with a newer one it does not cover.
    const windowSeconds = logic.historyRangeSeconds(state.historyRange);
    try {
        const history = await bridge.get_history(windowSeconds);
        state.history = Array.isArray(history) ? history : [];
        state.historyLoadedSeconds = windowSeconds;
        state.historyReceivedAt = Date.now();
    } catch (e) {
        // A failed one-shot fetch must not cache []: that empty sentinel is what
        // a genuine empty result uses, so the loaded-guard would be satisfied
        // forever and never retry. Reset to null so toggling History off and on
        // (or the next invalidation) re-fetches.
        state.history = null;
        state.historyLoadedSeconds = undefined;
    }

    state.historyLoading = false;
    afterHistoryLoaded();
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
