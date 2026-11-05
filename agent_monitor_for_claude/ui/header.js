'use strict';

/* --- window background --- */

// Report the content colour to Python so the native window matches it: the
// window edge shows through while the web view lags a resize.
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

/* --- window: the compact view ---
   The compact view is the narrow window that stays above all others; the full
   view never does. The page owns the preference (Python cannot read
   localStorage), so it is reported to the bridge on load and on every switch -
   as the compact size and the keep-on-top flag, which the bridge ignores when
   already in force. The compact layout itself is pure CSS (body.compact). */

function initCompactToggle() {
    try {
        state.compact = localStorage.getItem('amc-compact') === '1';
    } catch (e) {
        state.compact = false;
    }
    applyCompact();
    document.getElementById('compact-toggle').addEventListener('click', () => toggleCompact());
}

function toggleCompact(force) {
    state.compact = typeof force === 'boolean' ? force : !state.compact;
    try {
        localStorage.setItem('amc-compact', state.compact ? '1' : '0');
    } catch (e) { /* storage unavailable */ }
    applyCompact();
    if (state.last) {
        render(state.last);
    }
}

function applyCompact() {
    const on = state.compact;
    document.body.classList.toggle('compact', on);
    if (on) {
        state.detailOpen = false;
        document.body.classList.remove('detail-open');
    }
    refreshCompactButton();

    const bridge = apiBridge();
    if (!bridge) {
        return;
    }
    if (typeof bridge.set_compact === 'function') {
        Promise.resolve(bridge.set_compact(on)).catch(() => { /* window closing */ });
    }
    if (typeof bridge.set_on_top === 'function') {
        Promise.resolve(bridge.set_on_top(on)).catch(() => { /* window closing */ });
    }
}

// The button names what it switches to; called on init, on every switch, and
// once labels arrive.
function refreshCompactButton() {
    const labels = state.labels;
    const button = document.getElementById('compact-toggle');
    if (!button) {
        return;
    }
    const on = !!state.compact;
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.querySelector('.tool-label').textContent = (on ? labels.compact_exit : labels.compact) || '';
    const tip = on ? labels.compact_exit_tip : labels.compact_tip;
    if (tip) {
        button.dataset.tip = tip;
        button.setAttribute('aria-label', tip);
    }
}
