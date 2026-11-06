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

    // The window title names the app; the toolbar does not repeat it.
    document.title = state.labels.app_title;

    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.placeholder = state.labels.search_placeholder;
        searchInput.setAttribute('aria-label', state.labels.search_placeholder);
        // The shortcut that jumps here, on hover rather than in the box.
        searchInput.dataset.tip = (state.labels.key_ctrl || 'Ctrl') + ' K';
    }
    refreshSearchOptButtons();
    refreshCompactButton();
    refreshGroupSwitch();
    state.defaultEffort = typeof bootstrap.default_effort === 'string' ? bootstrap.default_effort : '';
}

function tickAges() {
    if (!state.last) {
        return;
    }

    fillTicks(document);
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

/* --- keyboard --- */

// Whether a key press is meant for a text field rather than for the list.
function typingTarget(target) {
    return Boolean(target && target.closest && target.closest('input, textarea, [contenteditable="true"]'));
}

// A focused button or link answers Enter itself - a section header, the
// grouping switch, a detail action - rather than raising the selected session.
function controlTarget(target) {
    return Boolean(target && target.closest && target.closest('button, a[href], select, [role="radio"], [role="button"]'));
}

function onKeyDown(event) {
    // A dialog owns the keys while it is open.
    if (document.getElementById('confirm-overlay')) {
        return;
    }
    const ctrl = event.ctrlKey || event.metaKey;

    if (ctrl && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        const input = document.getElementById('search-input');
        if (input) {
            input.focus();
            input.select();
        }
        return;
    }
    if (ctrl && event.key === '.') {
        event.preventDefault();
        toggleCompact();
        return;
    }
    if (typingTarget(event.target) || openMenuState) {
        return;
    }

    const session = selectedSession();
    const key = event.key;
    if (key === 'ArrowDown' || (!ctrl && key === 'j')) {
        event.preventDefault();
        moveSelection(1);
    } else if (key === 'ArrowUp' || (!ctrl && key === 'k')) {
        event.preventDefault();
        moveSelection(-1);
    } else if (key === 'Enter' && session && !controlTarget(event.target)) {
        event.preventDefault();
        if (hasWindow(session)) {
            focusSession(session);
        } else {
            selectSession(state.selectedKey, true);
        }
    } else if (ctrl && key.toLowerCase() === 'c' && session) {
        // Copy the id only when nothing is selected - a marked text keeps its
        // ordinary copy.
        const selection = window.getSelection ? String(window.getSelection()) : '';
        if (!selection) {
            event.preventDefault();
            runSessionAction('copy-id', session);
        }
    } else if (!ctrl && key === 't' && session) {
        event.preventDefault();
        runSessionAction('transcript', session);
    } else if (key === 'Escape' && state.detailOpen) {
        event.preventDefault();
        closeDetail();
    }
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
        // An unknown or missing key resolves to the default window, so a stale
        // stored value can never leave the listing unbounded.
        state.historyRange = logic.historyRange(localStorage.getItem('amc-history-range')).key;
        state.markdown = localStorage.getItem('amc-markdown') !== 'plain';
        state.collapsed = new Set(JSON.parse(localStorage.getItem('amc-collapsed') || '[]'));
        const searchOpts = JSON.parse(localStorage.getItem('amc-search-opts') || '{}');
        state.searchMatchCase = !!searchOpts.matchCase;
        state.searchWholeWord = !!searchOpts.wholeWord;
        state.searchRegex = !!searchOpts.regex;
    } catch (e) {
        state.historyRange = logic.DEFAULT_HISTORY_RANGE;
        state.collapsed = new Set();
        state.searchMatchCase = false;
        state.searchWholeWord = false;
        state.searchRegex = false;
    }
    // Each init step is isolated so a failure in one (e.g. a control the
    // markup no longer matches) cannot abort boot and freeze the loading
    // skeleton - the data still renders.
    try {
        reportWindowBackground();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initCompactToggle();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initListControls();
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
        const content = document.getElementById('content');
        content.addEventListener('click', onContentClick);
        content.addEventListener('dblclick', onContentDoubleClick);
        content.addEventListener('pointerdown', onGraphPointerDown);
        initSplitter();
        document.addEventListener('pointermove', onGraphPointerMove);
        document.addEventListener('pointerup', onGraphPointerUp);
        document.addEventListener('pointercancel', onGraphPointerUp);
        content.addEventListener('contextmenu', onContentContextMenu);
        document.addEventListener('contextmenu', onSelectionContextMenu);
        document.getElementById('detail').addEventListener('click', onDetailClick);
        // A <details> toggle does not bubble; the capture phase sees it.
        document.getElementById('detail').addEventListener('toggle', onStepToggle, true);
        document.addEventListener('keydown', onKeyDown);
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
    // If "Older" was left open in a previous session, load the past sessions now (the data source is ready by the time boot runs).
    if (state.filters.has('history')) {
        ensureHistoryLoaded();
    }
    setInterval(tick, state.pollInterval * 1000);
    setInterval(checkForChanges, FINGERPRINT_INTERVAL);
    setInterval(tickAges, 1000);
    gitOverviewLoop();
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
