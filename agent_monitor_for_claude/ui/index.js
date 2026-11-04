'use strict';

/* Agent Monitor for Claude - UI controller, start-up.

   Loaded last of the controller's scripts (see core.js and boot-scripts.js),
   so everything it calls is declared by the time it runs: it applies the
   bootstrap, drives the poll, and boots the page once a data source exists. */

/* --- bootstrap --- */

function applyBootstrap(bootstrap) {
    const received = bootstrap.labels || {};
    state.labels = Object.assign({}, DEFAULT_LABELS, received);
    state.labelsLoaded = Object.keys(received).length > 0;
    state.pricing = (bootstrap.pricing && typeof bootstrap.pricing === 'object') ? bootstrap.pricing : {};
    state.pollInterval = bootstrap.poll_interval || 5;

    const title = state.labels.app_title;
    document.getElementById('app-title').textContent = title;
    document.getElementById('app-subtitle').textContent = state.labels.subtitle;
    document.title = title;

    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.placeholder = state.labels.search_placeholder;
        searchInput.setAttribute('aria-label', state.labels.search_placeholder);
    }
    refreshSearchOptButtons();
    refreshWindowToggleButtons();

    const effortBadge = document.getElementById('effort-badge');
    state.defaultEffort = typeof bootstrap.default_effort === 'string' ? bootstrap.default_effort : '';
    if (bootstrap.default_effort) {
        effortBadge.textContent = fmt(state.labels.effort_badge, { level: bootstrap.default_effort });
        effortBadge.hidden = false;
    } else {
        effortBadge.hidden = true;
    }

    updateFilterControls();
    updateSortControls();
}

function tickAges() {
    if (!state.last) {
        return;
    }

    const now = Date.now();
    document.querySelectorAll('.age[data-age]').forEach((el) => {
        el.textContent = logic.formatAgeSince(Number(el.dataset.age), Number(el.dataset.ageAt), now, state.labels);
    });
}

async function checkForChanges() {
    const bridge = apiBridge();
    if (!bridge || typeof bridge.get_fingerprint !== 'function' || state.checking) {
        return;
    }

    state.checking = true;
    try {
        const fingerprint = await bridge.get_fingerprint();
        if (fingerprint !== state.fingerprint) {
            state.fingerprint = fingerprint;
            await tick();
            // Content changed: let an active search pick up any new matches.
            rescanForNewMatches();
        }
    } catch (err) {
        // Bridge hiccup - the regular full poll covers it.
    }
    state.checking = false;
}

/* --- lifecycle --- */

async function tick() {
    // Without a bridge and without dev mocks there is no data source yet -
    // keep the loading skeleton instead of rendering a false empty state.
    if (!apiBridge() && !mockMode()) {
        return;
    }

    // Recover translations if the first bootstrap ran before the bridge was ready.
    if (!state.labelsLoaded && apiBridge()) {
        try {
            applyBootstrap(await callBootstrap());
        } catch (err) {
            console.error('bootstrap retry failed', err);
        }
    }

    // The interval poll and a fingerprint-triggered tick can overlap, and their
    // snapshot promises race. Claim a generation before requesting: a result is
    // rendered only if no newer tick has started meanwhile, so an older, slower
    // snapshot never overwrites a newer one (nor resets the age base backward).
    const gen = ++state.snapshotGen;
    try {
        const snapshot = await callSnapshot();
        if (gen !== state.snapshotGen) {
            return;
        }
        state.receivedAt = Date.now();
        render(snapshot);
    } catch (err) {
        if (gen === state.snapshotGen) {
            reportUiError(err);
        }
    }
}

async function boot() {
    if (state.booted) {
        return;
    }
    state.booted = true;

    // loadFilters guards its own storage access and always returns a valid Set
    // (defaulting to DEFAULT_FILTER_KEYS, which excludes the off-by-default
    // History chip). Keep it out of the try below so a later corrupt value cannot
    // discard the user's filter selection - and never fall back to enabling every
    // chip, which would trigger the History scan unrequested.
    state.filters = loadFilters();
    try {
        state.sort = localStorage.getItem('amc-sort') || 'activity';
        state.sortDir = localStorage.getItem('amc-sort-dir') === 'desc' ? 'desc' : 'asc';
        // An unknown or missing key resolves to the default window, so a stale
        // stored value can never leave the listing unbounded.
        state.historyRange = logic.historyRange(localStorage.getItem('amc-history-range')).key;
        state.priorityOrder = localStorage.getItem('amc-priority-order') !== '0';
        state.collapsed = new Set(JSON.parse(localStorage.getItem('amc-collapsed') || '[]'));
        const searchOpts = JSON.parse(localStorage.getItem('amc-search-opts') || '{}');
        state.searchMatchCase = !!searchOpts.matchCase;
        state.searchWholeWord = !!searchOpts.wholeWord;
        state.searchRegex = !!searchOpts.regex;
    } catch (e) {
        state.sort = 'activity';
        state.sortDir = 'asc';
        state.historyRange = logic.DEFAULT_HISTORY_RANGE;
        state.priorityOrder = true;
        state.collapsed = new Set();
        state.searchMatchCase = false;
        state.searchWholeWord = false;
        state.searchRegex = false;
    }
    if (!SORT_VALUES[state.sort]) {
        state.sort = 'activity';
    }

    // Each init step is isolated so a failure in one (e.g. a control the
    // markup no longer matches) cannot abort boot and freeze the loading
    // skeleton - the data still renders.
    try {
        initTheme();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initWindowToggles();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initFilterControls();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initSortControls();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initSearch();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initTooltips();
    } catch (err) {
        reportUiError(err);
    }
    try {
        document.getElementById('content').addEventListener('click', onContentClick);
    } catch (err) {
        reportUiError(err);
    }
    if (!state.last) {
        renderLoading();
    }

    try {
        applyBootstrap(await callBootstrap());
    } catch (err) {
        reportUiError(err);
    }

    await tick();
    // If the history chip was left enabled in a previous session, load the past
    // sessions now (the data source is ready by the time boot runs).
    if (state.filters.has('history')) {
        ensureHistoryLoaded();
    }
    setInterval(tick, state.pollInterval * 1000);
    setInterval(checkForChanges, FINGERPRINT_INTERVAL);
    setInterval(tickAges, 1000);
}

function tryBoot() {
    if (state.booted) {
        return;
    }

    // Boot only with a real data source: the pywebview bridge (app) or the
    // mock data of a browser preview. Booting earlier would render a false
    // "no sessions" state from the empty fallback.
    if (apiBridge() || mockMode()) {
        boot();
    }
}

window.addEventListener('pywebviewready', tryBoot);
window.addEventListener('DOMContentLoaded', () => {
    // Show the loading skeleton immediately, even before the bridge exists.
    renderLoading();

    // Browser preview: no bridge is coming, so pull in the mock data and boot
    // once it has loaded. Never touched by the packaged app (served over http).
    if (!apiBridge() && devPreviewRequested()) {
        loadDevMock(tryBoot);
    }

    tryBoot();

    // Safety net: window.pywebview and its .api are injected at an unknown
    // point relative to this script, and the ready event may already have
    // fired before the listener above was registered.
    const probe = setInterval(() => {
        if (state.booted) {
            clearInterval(probe);
            return;
        }
        tryBoot();
    }, 250);
    setTimeout(() => clearInterval(probe), 30000);
});
