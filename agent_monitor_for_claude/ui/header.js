'use strict';

/* --- theme --- */

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function setThemeIcon(theme) {
    document.getElementById('theme-toggle').innerHTML = theme === 'dark' ? '&#9788;' : '&#9790;';
}

// Report the resolved content colour to Python so the native window matches it.
// Python only knows the system theme, not the page's stored preference, so
// without this a dark UI on a light system shows a light window edge while
// WebView2 lags a resize (and vice versa).
function reportWindowBackground() {
    const bridge = apiBridge();
    if (!bridge || typeof bridge.set_window_background !== 'function') {
        return;
    }
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
    if (bg) {
        Promise.resolve(bridge.set_window_background(bg)).catch(() => { /* window closing */ });
    }
}

function initTheme() {
    setThemeIcon(currentTheme());
    reportWindowBackground();
    document.getElementById('theme-toggle').addEventListener('click', () => {
        const next = currentTheme() === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try {
            localStorage.setItem('amc-theme', next);
        } catch (e) { /* storage unavailable */ }
        setThemeIcon(next);
        reportWindowBackground();
    });
}

/* --- window: compact view and keep-on-top ---
   Header toggles that change the window rather than the data. The page owns
   both preferences (Python cannot read localStorage), so each is reported to
   the bridge on load and on every click; the bridge ignores a report of the
   state already in force. The compact view is also a body class: the layout
   change is pure CSS, the bridge only resizes the window to match. */

const WINDOW_TOGGLES = [
    { id: 'compact-toggle', key: 'compact', tipKey: 'compact_view', storageKey: 'amc-compact',
        bridgeMethod: 'set_compact', bodyClass: 'compact' },
    { id: 'on-top-toggle', key: 'onTop', tipKey: 'keep_on_top', storageKey: 'amc-on-top',
        bridgeMethod: 'set_on_top', bodyClass: null },
];

function initWindowToggles() {
    WINDOW_TOGGLES.forEach((toggle) => {
        try {
            state[toggle.key] = localStorage.getItem(toggle.storageKey) === '1';
        } catch (e) {
            state[toggle.key] = false;
        }
        applyWindowToggle(toggle);

        document.getElementById(toggle.id).addEventListener('click', () => {
            state[toggle.key] = !state[toggle.key];
            try {
                localStorage.setItem(toggle.storageKey, state[toggle.key] ? '1' : '0');
            } catch (e) { /* storage unavailable */ }
            applyWindowToggle(toggle);
        });
    });
}

function applyWindowToggle(toggle) {
    const on = state[toggle.key];
    if (toggle.bodyClass) {
        document.body.classList.toggle(toggle.bodyClass, on);
        // Hidden cells measure 0, so the shared columns are re-measured for the
        // layout just switched to rather than at the next render.
        alignColumns();
    }
    refreshWindowToggleButtons();

    const bridge = apiBridge();
    if (bridge && typeof bridge[toggle.bridgeMethod] === 'function') {
        Promise.resolve(bridge[toggle.bridgeMethod](on)).catch(() => { /* window closing */ });
    }
}

function refreshWindowToggleButtons() {
    WINDOW_TOGGLES.forEach((toggle) => {
        const button = document.getElementById(toggle.id);
        if (!button) {
            return;
        }
        const on = !!state[toggle.key];
        button.classList.toggle('active', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
        const tip = state.labels[toggle.tipKey];
        if (tip) {
            button.dataset.tip = tip;
            button.setAttribute('aria-label', tip);
        }
    });
}
