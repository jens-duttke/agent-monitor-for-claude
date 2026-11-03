'use strict';

/* --- filtering --- */

// Each status chip is a checkbox, all on by default. A session shows only while
// its status band's chip is still active; unchecking a chip hides those
// sessions.
function matchesFilter(session) {
    const bucket = logic.sessionBucket(session);
    return bucket != null && state.filters.has(bucket);
}

function toggleFilter(key) {
    const wasActive = state.filters.has(key);
    if (wasActive) {
        state.filters.delete(key);
    } else {
        state.filters.add(key);
    }
    persistFilters();
    updateFilterControls();

    // Turning the history chip on triggers the (potentially second-long) scan of
    // past sessions - lazily, off the UI thread, so the page never blocks. When
    // it arrives, afterHistoryLoaded re-runs any active search over the widened
    // scope and re-renders.
    if (key === 'history' && !wasActive && state.history === null) {
        ensureHistoryLoaded();
        return;
    }

    // A chip toggle changes which sessions are in view - and thus the search
    // scope - so re-run any active search; otherwise just re-render.
    if (state.search.trim().length >= SEARCH_MIN_CHARS) {
        runSearch();
    } else if (state.last) {
        render(state.last);
    }
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
// from the cache; only a wider one needs the backend again. Selecting a range
// in the filter list also switches the chip on: the options are otherwise a
// dead end while the chip is off - the user picks a window, and nothing appears.
// The search's widening offer passes activateChip false: it asked to look
// further back, not to list every past session, and switching the chip on would
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
    updateFilterControls();

    if (needsFetch) {
        state.history = null;
        state.historyLoadedSeconds = undefined;
        ensureHistoryLoaded();
        return;
    }

    // The visible set changed, so an active search has to re-run over the new
    // scope - exactly as a chip toggle does.
    if (state.search.trim().length >= SEARCH_MIN_CHARS) {
        runSearch();
    } else if (state.last) {
        render(state.last);
    }
}

// Fetch past sessions once, the first time the history chip is enabled, and
// cache them. The bridge call runs on a pywebview worker thread, so awaiting it
// does not block the WebView: the live overview stays interactive and a loading
// note shows until the scan returns.
async function ensureHistoryLoaded() {
    if (state.history !== null || state.historyLoading) {
        // Already loading (or loaded): still reflect the current chip/loading
        // state. Toggling History on again during the in-flight first load lands
        // here, and without this render the chip would look inactive with no
        // loading note until the fetch lands or the next poll.
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

// Restore the persisted selection as a set of active bucket keys. Every status
// chip is on by default, so with nothing stored all chips are active and every
// session shows; a stored selection restores exactly which chips the user left
// on. Tolerates the legacy single-value format ("all" meant show-everything;
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
    // back to the default chips active.
    return active.size > 0 ? active : new Set(DEFAULT_FILTER_KEYS);
}

// Count sessions per filter bucket. With an `includeSession` predicate (the
// search match), only matching sessions are counted, so the chip counts track
// what the search has narrowed the view to.
function countByFilter(projects, includeSession) {
    const counts = { needs: 0, idle: 0, working: 0, background: 0, errored: 0, interrupted: 0, quiet: 0, new: 0, history: 0 };
    for (const project of projects) {
        for (const session of project.sessions) {
            if (includeSession && !includeSession(session)) {
                continue;
            }
            const bucket = logic.sessionBucket(session);
            if (bucket) {
                counts[bucket] += 1;
            }
        }
    }
    return counts;
}

// A funnel in the search field's stroke style, marking the dropdown as the filter.
const FUNNEL_SVG = '<svg class="trigger-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"></path></svg>';

// The filters are one dropdown. Collapsed, its trigger names the active ones;
// open, it lists every status band below each other with a checkbox, its colour
// dot (which doubles as the key to the row dots) and its session count. Both are
// updated in place on every render rather than rebuilt, so a ticked box keeps
// its focus and a tooltip its target while the counts change.
function initFilterControls() {
    initDropdownTrigger(document.getElementById('filter-trigger'), FUNNEL_SVG, openFilterMenu);
    updateFilterControls();
}

function renderFilters(counts) {
    state.filterCounts = counts;
    updateFilterControls();
}

function updateFilterControls() {
    const label = document.querySelector('#filter-trigger .trigger-label');
    if (label) {
        const rangeLabel = state.labels[logic.historyRange(state.historyRange).label] || state.historyRange;
        label.textContent = logic.filterSummary(FILTER_DEFS, state.filters, state.labels, rangeLabel);
    }
    if (openMenuState && openMenuState.type === 'filters') {
        syncFilterMenu(openMenuState.el);
    }
}

function openFilterMenu(trigger, focusFirst) {
    if (!claimMenuSlot(trigger)) {
        return;
    }

    const el = document.createElement('div');
    el.className = 'menu filter-menu';
    el.setAttribute('role', 'group');
    el.innerHTML = FILTER_DEFS.map(filterOptionMarkup).join('');

    el.querySelectorAll('input[data-filter]').forEach((input) => {
        input.addEventListener('change', () => toggleFilter(input.dataset.filter));
    });
    el.querySelectorAll('button[data-range]').forEach((button) => {
        button.addEventListener('click', () => setHistoryRange(button.dataset.range));
    });

    syncFilterMenu(el);
    showMenu(el, trigger, { type: 'filters', align: 'left' });

    if (focusFirst) {
        const first = el.querySelector('input[data-filter]');
        if (first) {
            first.focus();
        }
    }
}

// One line of the filter list. Its checked state, count and range selection are
// left to syncFilterMenu, the one place that reads them from state.
function filterOptionMarkup(def) {
    const label = state.labels[def.label] || def.key;
    const tip = def.tip ? (state.labels[def.tip] || '') : '';
    const markup = '<label' + attr('class', 'menu-option') + (tip ? attr('data-tip', tip) : '') + '>'
        + '<input' + attr('type', 'checkbox') + attr('data-filter', def.key) + '>'
        + '<span' + attr('class', 'filter-dot ' + (def.dot || '')) + '></span>'
        + '<span class="menu-label">' + esc(label) + '</span>'
        + '<span' + attr('class', 'count') + attr('data-count', def.key) + '></span>'
        + '</label>';

    return def.key === 'history' ? markup + historyRangeMarkup() : markup;
}

// How far back the History filter reaches, as a row of options under its
// checkbox. Picking one also ticks the box (setHistoryRange): choosing a window
// and then seeing nothing would be a dead end.
function historyRangeMarkup() {
    const tip = state.labels.history_range_tip || '';
    const buttons = logic.HISTORY_RANGES.map((range) => '<button'
        + attr('type', 'button') + attr('class', 'filter-range') + attr('data-range', range.key) + '>'
        + esc(state.labels[range.label] || range.key)
        + '</button>').join('');

    return '<div' + attr('class', 'filter-ranges') + attr('role', 'group') + (tip ? attr('data-tip', tip) : '') + '>'
        + buttons + '</div>';
}

function syncFilterMenu(el) {
    el.querySelectorAll('input[data-filter]').forEach((input) => {
        const active = state.filters.has(input.dataset.filter);
        input.checked = active;
        input.closest('.menu-option').classList.toggle('active', active);
    });
    // No badge at zero: before the first history scan there is no number to
    // show at all, and an empty pill would read as a rendering glitch.
    el.querySelectorAll('[data-count]').forEach((badge) => {
        const count = state.filterCounts[badge.dataset.count];
        badge.textContent = count > 0 ? String(count) : '';
    });
    el.querySelectorAll('button[data-range]').forEach((button) => {
        const active = button.dataset.range === state.historyRange;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}
