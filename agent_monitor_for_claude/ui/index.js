'use strict';

/* Agent Monitor for Claude - UI controller.

   The Python bridge is a pure data provider: get_snapshot returns raw session
   records, and all derivation (status, formatting, grouping, sorting) happens
   in logic.js. This controller consumes that logic, renders the overview with
   granular DOM reconciliation (no full rebuilds - open menus and scroll
   position survive a refresh), and owns the DOM/bridge side effects. Opened
   directly in a browser (a file:// page, or ?mock) there is no bridge, so it
   loads the unbundled dev-mock.js and renders from that mock data instead. */

const logic = window.AMC_LOGIC;

// A UI error would otherwise be swallowed inside WebView2 (no terminal, no
// console). Surface it: forward to the Python bridge (-> stderr) and show it
// in the window instead of leaving the loading skeleton up forever.
function reportUiError(detail) {
    const message = detail && detail.stack ? detail.stack : String(detail);
    try {
        const bridge = (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
        if (bridge && typeof bridge.log === 'function') {
            bridge.log(message);
        }
    } catch (e) { /* bridge unavailable */ }
    try {
        const content = document.getElementById('content');
        if (content) {
            content.innerHTML = '<div class="empty error">UI error - see details below:\n\n'
                + message.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</div>';
        }
    } catch (e) { /* DOM unavailable */ }
}

window.addEventListener('error', (event) => reportUiError(event.error || event.message));
window.addEventListener('unhandledrejection', (event) => reportUiError(event.reason));

const state = {
    labels: {},
    labelsLoaded: false,
    pricing: {},
    pollInterval: 5,
    filters: new Set(),
    sort: 'activity',
    sortDir: 'asc',
    priorityOrder: true,
    collapsed: new Set(),
    last: null,
    receivedAt: null,
    fingerprint: null,
    checking: false,
    booted: false,
    // Past (non-live) sessions, fetched on demand the first time the history
    // chip is enabled and cached thereafter (dead sessions do not change). null
    // means "not fetched yet"; historyLoading guards against a double fetch and
    // drives the loading note.
    history: null,
    historyLoading: false,
};

// Sort options: key -> label key. Values are computed per session below.
const SORT_DEFS = [
    ['activity', 'sort_activity'],
    ['usage', 'sort_usage'],
    ['model', 'sort_model'],
    ['host', 'sort_host'],
    ['status', 'sort_status'],
];

// Interval (ms) for the cheap change-fingerprint probe between full polls.
const FINGERPRINT_INTERVAL = 1000;

// English fallbacks so the UI never renders empty text, even if the
// bridge delivers its translations late.
const DEFAULT_LABELS = {
    app_title: 'Agent Monitor for Claude',
    subtitle: 'Live status of your Claude Code agents',
    status_working: 'Working',
    status_processing: 'Background',
    status_interrupted: 'Interrupted',
    status_errored: 'Error',
    status_usage_limit: 'Usage limit reached',
    status_awaiting_input: 'Idle',
    status_awaiting_permission: 'Permission needed',
    status_needs_you: 'Waiting for you',
    status_question: 'Question for you',
    status_plan_review: 'Plan review',
    status_new: 'New',
    status_completed: 'Finished',
    status_unknown: 'Unknown',
    feedback_needed: '{count} waiting for your answer',
    sort_activity: 'Last activity',
    sort_usage: 'Usage',
    sort_model: 'Model',
    sort_host: 'Host',
    sort_status: 'Status',
    priority_order: 'Priority order',
    priority_order_hint: 'Sort projects by attention - the ones that need you first',
    sort_direction_hint: 'Switch between ascending and descending order',
    empty_state: 'No active Claude Code agents.',
    empty_filter: 'No agents match this filter.',
    last_activity: 'Last activity {age} ago',
    no_activity: 'No activity yet',
    tool_running: 'tool running',
    kind_interactive: 'Interactive',
    kind_background: 'Background',
    filter_needs: 'Needs you',
    filter_errored: 'Error',
    filter_interrupted: 'Interrupted',
    filter_idle: 'Idle',
    filter_working: 'Working',
    filter_background: 'Background',
    filter_quiet: 'Quiet',
    filter_needs_tip: 'The agent stopped and needs you - to grant a permission, answer a question, or approve a plan.',
    filter_errored_tip: "An API error or usage limit stopped the last run - it can't continue until that's resolved.",
    filter_interrupted_tip: "You stopped the agent mid-task, so it's your turn again - nothing is running.",
    filter_new_tip: "A just-started session that hasn't written anything to its transcript yet.",
    filter_idle_tip: 'The agent finished and is waiting for your next message. Nothing is required.',
    filter_working_tip: 'The agent is busy - thinking, running a tool, or working on your prompt.',
    filter_background_tip: 'The agent finished, but a subagent or background process is still running.',
    filter_quiet_tip: 'Finished or inactive sessions - nothing needs you and nothing is running.',
    age_seconds: '{s}s',
    age_minutes: '{m}m',
    age_hours: '{h}h {m}m',
    age_days: '{d}d',
    token_summary: '{input} in · {output} out',
    token_cache_read: '{cache_read} cache read',
    token_cache_5m: '{cache_5m} 5m write',
    token_cache_1h: '{cache_1h} 1h write',
    token_cache_write: '{cache_write} cache write',
    effort_badge: 'Default effort: {level}',
    subagents_running: 'Running subagents: {count}',
    subagents_finished: 'Recently finished: {count}',
    processes_running: 'Background processes: {count}',
    row_menu: 'More actions',
    copy_session_id: 'Copy session ID',
    copied: 'Copied to clipboard',
    open_in_explorer: 'Open in Explorer',
    filter_history: 'History',
    filter_history_tip: 'Past sessions that are no longer running (loaded on demand)',
    history_loading: 'Loading past sessions…',
    delete_session: 'Delete session',
    delete_confirm_title: 'Delete this session?',
    delete_confirm_body: 'This permanently removes the session transcript and its subagent files from disk. This cannot be undone.',
    delete_confirm_ok: 'Delete',
    cancel: 'Cancel',
    deleted: 'Session deleted',
    delete_failed: 'Could not delete the session',
};

// One chip per status color, attention-first: the states that want you
// (blocked, errored, interrupted, new, idle) come before the ones that don't
// (busy, background, quiet). Each chip is a checkbox for its status band: all
// on by default, and unchecking one hides those sessions. Each dot teaches its
// color, so the chips also serve as the status legend.
const FILTER_DEFS = [
    { key: 'needs', label: 'filter_needs', tip: 'filter_needs_tip', dot: 'dot-needs' },
    { key: 'errored', label: 'filter_errored', tip: 'filter_errored_tip', dot: 'dot-errored' },
    { key: 'interrupted', label: 'filter_interrupted', tip: 'filter_interrupted_tip', dot: 'dot-interrupted' },
    { key: 'new', label: 'status_new', tip: 'filter_new_tip', dot: 'dot-new' },
    { key: 'idle', label: 'filter_idle', tip: 'filter_idle_tip', dot: 'dot-idle' },
    { key: 'working', label: 'filter_working', tip: 'filter_working_tip', dot: 'dot-working' },
    { key: 'background', label: 'filter_background', tip: 'filter_background_tip', dot: 'dot-background' },
    { key: 'quiet', label: 'filter_quiet', tip: 'filter_quiet_tip', dot: 'dot-quiet' },
    // Off by default: enabling it triggers the on-demand scan of past sessions.
    { key: 'history', label: 'filter_history', tip: 'filter_history_tip', dot: 'dot-history', offByDefault: true },
];

// The status buckets the chips can select - the valid, persistable filter keys.
const FILTER_KEYS = new Set(FILTER_DEFS.map((def) => def.key));

// The chips active on a first launch: every chip except the ones that opt out
// (history), so the potentially large history scan only runs once the user asks
// for it.
const DEFAULT_FILTER_KEYS = FILTER_DEFS.filter((def) => !def.offByDefault).map((def) => def.key);

function apiBridge() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : null;
}

function mockMode() {
    return !!window.__MOCK_BOOTSTRAP__;
}

// A browser preview (no pywebview bridge) renders from mock data. That data
// lives in dev-mock.js, a sibling deliberately never bundled (see the spec), so
// the packaged app - always served over http with the bridge present - has no
// code path that reads fabricated session data. The preview is requested only
// from a file:// page or an explicit ?mock query, never in the packaged app.
function devPreviewRequested() {
    try {
        return location.protocol === 'file:' || new URLSearchParams(location.search).has('mock');
    } catch (e) {
        return false;
    }
}

let devMockLoading = false;

function loadDevMock(done) {
    if (devMockLoading) {
        return;
    }
    devMockLoading = true;
    const script = document.createElement('script');
    script.src = 'dev-mock.js';
    // A missing file (e.g. the packaged app, where it is never shipped) simply
    // leaves the loading skeleton up - never an error the user has to see.
    script.onload = done;
    script.onerror = done;
    document.head.appendChild(script);
}

async function callBootstrap() {
    const bridge = apiBridge();
    if (bridge) {
        return bridge.get_bootstrap();
    }
    return window.__MOCK_BOOTSTRAP__ || { labels: {}, poll_interval: 5 };
}

async function callSnapshot() {
    const bridge = apiBridge();
    if (bridge) {
        return bridge.get_snapshot();
    }
    return window.__MOCK_SNAPSHOT__ || { generated_at: '', sessions: [] };
}

function esc(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function fmt(template, values) {
    return String(template || '').replace(/\{(\w+)\}/g, (_, key) => (values[key] != null ? values[key] : ''));
}

function ageLabel(session) {
    return session.age_seconds == null ? '' : logic.formatAge(session.age_seconds, state.labels);
}

// Local calendar date as YYYY-MM-DD, used to pick the active price schedule.
function todayIso() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    return `${now.getFullYear()}-${month}-${day}`;
}

/* --- tooltip ---

   The one and only tooltip in the app. Never use the browser's native `title`
   tooltip (ugly, uncontrollable delay/placement, no theming) - give an element a
   `data-tip` attribute (newlines become line breaks) and this shared, themed,
   HTML tooltip shows it on hover. A single delegated listener handles every
   `data-tip` element, so reconciled rows need no per-node wiring. */
const TOOLTIP_DELAY = 350;

let tooltipEl = null;
let tooltipTarget = null;
let tooltipTimer = null;

function ensureTooltip() {
    if (!tooltipEl || !tooltipEl.isConnected) {
        tooltipEl = document.createElement('div');
        tooltipEl.className = 'tooltip';
        tooltipEl.setAttribute('role', 'tooltip');
        // The visible tooltip has pointer-events: auto, so it catches the whole
        // press instead of leaking it to the element behind it; on the click that
        // completes on the tooltip we just dismiss, acting on nothing underneath.
        tooltipEl.addEventListener('click', hideTooltip);
        tooltipEl.addEventListener('pointerleave', hideTooltip);
        document.body.appendChild(tooltipEl);
    }
    return tooltipEl;
}

function positionTooltip(target) {
    const tip = tooltipEl;
    const rect = target.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;

    let top = rect.bottom + gap;
    if (top + height + margin > window.innerHeight) {
        top = rect.top - height - gap;
    }
    let left = Math.min(rect.left, window.innerWidth - width - margin);
    left = Math.max(margin, left);
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(Math.max(margin, top)) + 'px';
}

function hideTooltip() {
    clearTimeout(tooltipTimer);
    tooltipTimer = null;
    tooltipTarget = null;
    if (tooltipEl) {
        tooltipEl.classList.remove('show');
    }
}

function initTooltips() {
    ensureTooltip();
    document.addEventListener('pointerover', (event) => {
        const target = event.target.closest ? event.target.closest('[data-tip]') : null;
        if (!target || target === tooltipTarget) {
            return;
        }
        clearTimeout(tooltipTimer);
        tooltipTarget = target;
        tooltipTimer = setTimeout(() => {
            if (tooltipTarget !== target || !target.isConnected || !target.dataset.tip) {
                return;
            }
            const tip = ensureTooltip();
            tip.textContent = target.dataset.tip;   // .tooltip has white-space: pre-line
            positionTooltip(target);
            tip.classList.add('show');
        }, TOOLTIP_DELAY);
    });
    document.addEventListener('pointerout', (event) => {
        const target = event.target.closest ? event.target.closest('[data-tip]') : null;
        if (!target || target !== tooltipTarget) {
            return;
        }
        // Moving onto the target's own children, or onto the tooltip itself,
        // must not dismiss it - the tooltip stays reachable so it can be clicked.
        const into = event.relatedTarget;
        if (into && (target.contains(into) || (tooltipEl && tooltipEl.contains(into)))) {
            return;
        }
        hideTooltip();
    });
    // A moved/scrolled target leaves a stale tooltip behind - drop it.
    document.addEventListener('scroll', hideTooltip, true);
    window.addEventListener('resize', hideTooltip);
}

/* --- theme --- */

function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function setThemeIcon(theme) {
    document.getElementById('theme-toggle').innerHTML = theme === 'dark' ? '&#9788;' : '&#9790;';
}

function initTheme() {
    setThemeIcon(currentTheme());
    document.getElementById('theme-toggle').addEventListener('click', () => {
        const next = currentTheme() === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try {
            localStorage.setItem('amc-theme', next);
        } catch (e) { /* storage unavailable */ }
        setThemeIcon(next);
    });
}

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

    const effortBadge = document.getElementById('effort-badge');
    if (bootstrap.default_effort) {
        effortBadge.textContent = fmt(state.labels.effort_badge, { level: bootstrap.default_effort });
        effortBadge.hidden = false;
    } else {
        effortBadge.hidden = true;
    }

    updateSortTrigger();
    updateSortDirButton();
    updatePriorityToggle();
}

/* --- sorting (view-level; uses pure helpers from logic.js) --- */

const SORT_VALUES = {
    activity: (session) => (session.age_seconds == null ? Infinity : session.age_seconds),
    usage: (session) => session.usage_total || 0,
    model: (session) => logic.modelRank(session.model),
    host: (session) => ((session.host || '￿') + (session.via_cli ? ' cli' : '')).toLowerCase(),
    status: (session) => logic.STATUS_ORDER[session.status] ?? 99,
};

function sortSessions(sessions) {
    const value = SORT_VALUES[state.sort] || SORT_VALUES.activity;
    const direction = state.sortDir === 'desc' ? -1 : 1;

    return [...sessions].sort((a, b) => {
        // In priority order, status is the primary key within a project (most
        // urgent first, always ascending regardless of the sort direction); the
        // selected sort criterion only breaks ties among sessions of equal
        // status.
        if (state.priorityOrder) {
            const rankA = logic.STATUS_ORDER[a.status] ?? 99;
            const rankB = logic.STATUS_ORDER[b.status] ?? 99;
            if (rankA !== rankB) { return rankA - rankB; }
        }

        const va = value(a);
        const vb = value(b);
        if (va < vb) { return -direction; }
        if (va > vb) { return direction; }
        return 0;
    });
}

function updateSortDirButton() {
    const button = document.getElementById('sort-dir');
    if (!button) {
        return;
    }
    button.innerHTML = state.sortDir === 'desc' ? '&#9660;' : '&#9650;';
    button.dataset.tip = state.labels.sort_direction_hint || '';
}

function sortLabel(key) {
    const def = SORT_DEFS.find(([sortKey]) => sortKey === key);
    return def ? (state.labels[def[1]] || key) : key;
}

function updateSortTrigger() {
    const trigger = document.getElementById('sort-trigger');
    if (trigger) {
        trigger.innerHTML = '<span class="sort-trigger-label">' + esc(sortLabel(state.sort)) + '</span>'
            + '<svg class="sort-trigger-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
            + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            + '<path d="m6 9 6 6 6-6"></path></svg>';
    }
}

function initSortControls() {
    const trigger = document.getElementById('sort-trigger');
    const dirButton = document.getElementById('sort-dir');

    trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        const items = SORT_DEFS.map(([key, labelKey]) => ({
            key,
            label: state.labels[labelKey] || key,
            active: key === state.sort,
        }));
        openMenu(trigger, items, (key) => {
            state.sort = key;
            try {
                localStorage.setItem('amc-sort', state.sort);
            } catch (e) { /* storage unavailable */ }
            updateSortTrigger();
            if (state.last) {
                render(state.last);
            }
        }, { type: 'sort' });
    });

    dirButton.addEventListener('click', () => {
        state.sortDir = state.sortDir === 'desc' ? 'asc' : 'desc';
        try {
            localStorage.setItem('amc-sort-dir', state.sortDir);
        } catch (e) { /* storage unavailable */ }
        updateSortDirButton();
        if (state.last) {
            render(state.last);
        }
    });

    updateSortTrigger();
    updateSortDirButton();
}

// Cross-project ordering toggle. When on (the default), projects are grouped
// into attention bands so the ones that need you sit on top; when off, they are
// a plain alphabetical list. It is separate from the sort control above, which
// orders the sessions within each project.
function updatePriorityToggle() {
    const button = document.getElementById('priority-toggle');
    if (!button) {
        return;
    }
    button.textContent = state.labels.priority_order || 'Priority order';
    button.dataset.tip = state.labels.priority_order_hint || '';
    button.classList.toggle('active', state.priorityOrder);
    button.setAttribute('aria-pressed', state.priorityOrder ? 'true' : 'false');
}

function initPriorityToggle() {
    const button = document.getElementById('priority-toggle');
    if (!button) {
        return;
    }
    button.addEventListener('click', () => {
        state.priorityOrder = !state.priorityOrder;
        try {
            localStorage.setItem('amc-priority-order', state.priorityOrder ? '1' : '0');
        } catch (e) { /* storage unavailable */ }
        updatePriorityToggle();
        if (state.last) {
            render(state.last);
        }
    });
    updatePriorityToggle();
}

/* --- fancy menu (sort dropdown + per-row meatball) --- */

// A single floating menu is reused for the sort dropdown and every row's
// meatball menu; only one can be open at a time. The state remembers what the
// menu is bound to (the sort trigger, or a specific session's row button) so a
// re-render can re-anchor it instead of tearing it down.
let openMenuState = null;

function closeMenu() {
    const menu = openMenuState;
    if (!menu) {
        return;
    }
    openMenuState = null;
    document.removeEventListener('click', menu.onDocClick, true);
    document.removeEventListener('keydown', menu.onKey, true);
    document.removeEventListener('scroll', menu.onViewportChange, true);
    window.removeEventListener('resize', menu.onViewportChange);
    if (menu.anchorEl) {
        menu.anchorEl.classList.remove('menu-open');
    }
    menu.el.remove();
}

function openMenu(anchor, items, onSelect, context) {
    // Clicking the same trigger again toggles the menu shut.
    const toggleShut = openMenuState && openMenuState.anchorEl === anchor;
    closeMenu();
    if (toggleShut) {
        return;
    }

    const el = document.createElement('div');
    el.className = 'menu';
    el.setAttribute('role', 'menu');

    items.forEach((item) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'menu-item' + (item.active ? ' active' : '') + (item.danger ? ' danger' : '');
        button.setAttribute('role', 'menuitem');
        button.innerHTML = '<span class="menu-check" aria-hidden="true">&#10003;</span>'
            + '<span class="menu-label"></span>';
        button.querySelector('.menu-label').textContent = item.label;
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            closeMenu();
            onSelect(item.key);
        });
        el.appendChild(button);
    });

    document.body.appendChild(el);
    anchor.classList.add('menu-open');

    const menu = {
        el,
        anchorEl: anchor,
        type: context.type,
        sessionId: context.sessionId || null,
    };

    menu.onDocClick = (event) => {
        if (!el.contains(event.target) && !menu.anchorEl.contains(event.target) && event.target !== menu.anchorEl) {
            closeMenu();
        }
    };
    menu.onKey = (event) => {
        if (event.key === 'Escape') {
            closeMenu();
        }
    };
    // Scroll / resize move the anchor, so follow it. Crucially this never
    // closes the menu - a data refresh (which does not scroll) must leave it
    // open; re-anchoring after a render is handled by syncOpenMenu.
    menu.onViewportChange = () => {
        if (menu.anchorEl && menu.anchorEl.isConnected) {
            positionMenu(el, menu.anchorEl);
        }
    };

    openMenuState = menu;

    // Defer the outside-click listener so the click that opened the menu does
    // not immediately close it.
    setTimeout(() => {
        if (openMenuState === menu) {
            document.addEventListener('click', menu.onDocClick, true);
        }
    }, 0);
    document.addEventListener('keydown', menu.onKey, true);
    document.addEventListener('scroll', menu.onViewportChange, true);
    window.addEventListener('resize', menu.onViewportChange);

    positionMenu(el, anchor);
}

// Called after each render. The DOM node a menu was anchored to may have moved
// (rows reorder) but is reused, so re-bind to the current node and reposition.
// A row menu whose session has disappeared is the only case that closes a menu
// automatically.
function syncOpenMenu() {
    const menu = openMenuState;
    if (!menu) {
        return;
    }

    let anchor = null;
    if (menu.type === 'sort') {
        anchor = document.getElementById('sort-trigger');
    } else if (menu.type === 'row') {
        anchor = findRowMenuButton(menu.sessionId);
    }

    if (!anchor) {
        closeMenu();
        return;
    }

    if (anchor !== menu.anchorEl) {
        menu.anchorEl.classList.remove('menu-open');
        anchor.classList.add('menu-open');
        menu.anchorEl = anchor;
    }
    positionMenu(menu.el, anchor);
}

function findRowMenuButton(sessionId) {
    const buttons = document.querySelectorAll('.row-menu-btn');
    for (const button of buttons) {
        if (button.dataset.session === sessionId) {
            return button;
        }
    }
    return null;
}

function positionMenu(menu, anchor) {
    const rect = anchor.getBoundingClientRect();
    const margin = 6;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;

    // Right-align the menu under the anchor, then clamp to the viewport.
    let left = rect.right - width;
    if (left < margin) {
        left = Math.min(rect.left, window.innerWidth - width - margin);
    }
    left = Math.max(margin, left);

    let top = rect.bottom + 4;
    if (top + height > window.innerHeight - margin) {
        const above = rect.top - height - 4;
        top = above >= margin ? above : Math.max(margin, window.innerHeight - height - margin);
    }

    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
}

/* --- clipboard + toast --- */

async function copyToClipboard(text) {
    if (!text) {
        return;
    }

    const bridge = apiBridge();
    if (bridge && typeof bridge.copy_text === 'function') {
        try {
            const ok = await bridge.copy_text(text);
            if (ok) {
                toast(state.labels.copied);
                return;
            }
        } catch (e) { /* fall through to the browser API */ }
    }

    try {
        await navigator.clipboard.writeText(text);
        toast(state.labels.copied);
    } catch (e) {
        // No clipboard access (e.g. plain dev harness) - nothing more to do.
    }
}

let toastTimer = null;

function toast(message) {
    if (!message) {
        return;
    }

    let el = document.getElementById('toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'toast';
        el.className = 'toast';
        document.body.appendChild(el);
    }

    el.textContent = message;
    el.classList.add('show');
    if (toastTimer) {
        clearTimeout(toastTimer);
    }
    toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

/* --- delete a past session (the sole write action) --- */

// Ask before deleting, then call the bridge. On success the session is dropped
// from the cached history and the view re-renders; the deletion itself runs on
// a worker thread, so awaiting it never blocks the window.
function confirmDeleteSession(sessionId, cwd) {
    if (!sessionId) {
        return;
    }
    showConfirm(state.labels.delete_confirm_title, state.labels.delete_confirm_body, state.labels.delete_confirm_ok, async () => {
        const bridge = apiBridge();
        if (!bridge || typeof bridge.delete_session !== 'function') {
            return;
        }

        let ok = false;
        try {
            ok = await bridge.delete_session(sessionId, cwd);
        } catch (e) {
            ok = false;
        }

        if (ok) {
            if (Array.isArray(state.history)) {
                state.history = state.history.filter((session) => session.session_id !== sessionId);
            }
            toast(state.labels.deleted);
            if (state.last) {
                render(state.last);
            }
        } else {
            toast(state.labels.delete_failed);
        }
    });
}

// A themed, self-contained confirmation modal (the native confirm dialog is
// unthemed and blocking, and the app deliberately avoids native chrome). Escape
// or a backdrop click cancels; only the primary button runs onConfirm.
function showConfirm(title, body, okLabel, onConfirm) {
    closeConfirm();

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'confirm-overlay';
    overlay.innerHTML = '<div class="modal" role="dialog" aria-modal="true">'
        + '<div class="modal-title"></div>'
        + '<div class="modal-body"></div>'
        + '<div class="modal-actions">'
        +     '<button class="modal-btn" type="button" data-act="cancel"></button>'
        +     '<button class="modal-btn danger" type="button" data-act="ok"></button>'
        + '</div></div>';

    overlay.querySelector('.modal-title').textContent = title || '';
    overlay.querySelector('.modal-body').textContent = body || '';

    const cancelBtn = overlay.querySelector('[data-act="cancel"]');
    const okBtn = overlay.querySelector('[data-act="ok"]');
    cancelBtn.textContent = state.labels.cancel || 'Cancel';
    okBtn.textContent = okLabel || state.labels.delete_confirm_ok || 'Delete';

    cancelBtn.addEventListener('click', closeConfirm);
    okBtn.addEventListener('click', () => {
        closeConfirm();
        onConfirm();
    });
    overlay.addEventListener('click', (event) => {
        if (event.target === overlay) {
            closeConfirm();
        }
    });

    document.addEventListener('keydown', onConfirmKey, true);
    document.body.appendChild(overlay);
    okBtn.focus();
}

function onConfirmKey(event) {
    if (event.key === 'Escape') {
        closeConfirm();
    }
}

function closeConfirm() {
    const overlay = document.getElementById('confirm-overlay');
    if (overlay) {
        overlay.remove();
    }
    document.removeEventListener('keydown', onConfirmKey, true);
}

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

    // Turning the history chip on is what triggers the (potentially second-long)
    // scan of past sessions - lazily, off the UI thread, so the page never
    // blocks. ensureHistoryLoaded re-renders when the data arrives.
    if (key === 'history' && !wasActive) {
        ensureHistoryLoaded();
    }

    if (state.last) {
        render(state.last);
    }
}

// Fetch past sessions once, the first time the history chip is enabled, and
// cache them. The bridge call runs on a pywebview worker thread, so awaiting it
// does not block the WebView: the live overview stays interactive and a loading
// note shows until the scan returns.
async function ensureHistoryLoaded() {
    if (state.history !== null || state.historyLoading) {
        return;
    }

    const bridge = apiBridge();
    if (!bridge || typeof bridge.get_history !== 'function') {
        // Browser preview: use the fabricated history if dev-mock provided any.
        // Without a bridge and not in mock mode the data source is not ready yet;
        // leave history null so a later call (a re-toggle, or boot) retries.
        if (mockMode()) {
            state.history = Array.isArray(window.__MOCK_HISTORY__) ? window.__MOCK_HISTORY__ : [];
            if (state.last) {
                render(state.last);
            }
        }
        return;
    }

    state.historyLoading = true;
    if (state.last) {
        render(state.last);
    }

    try {
        const history = await bridge.get_history();
        state.history = Array.isArray(history) ? history : [];
    } catch (e) {
        state.history = [];
    }

    state.historyLoading = false;
    if (state.last) {
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

function countByFilter(projects) {
    const counts = { all: 0, needs: 0, idle: 0, working: 0, background: 0, errored: 0, interrupted: 0, quiet: 0, new: 0, history: 0 };
    for (const project of projects) {
        for (const session of project.sessions) {
            counts.all += 1;
            const bucket = logic.sessionBucket(session);
            if (bucket) {
                counts[bucket] += 1;
            }
        }
    }
    return counts;
}

function renderFilters(counts) {
    const container = document.getElementById('filters');

    container.innerHTML = FILTER_DEFS.map((def) => {
        const active = state.filters.has(def.key);
        const dot = def.dot ? ' ' + def.dot : '';
        const label = state.labels[def.label] || def.key;
        const tip = def.tip ? (state.labels[def.tip] || '') : '';
        const count = counts[def.key];
        return '<button class="filter-chip' + dot + (active ? ' active' : '') + '" data-filter="' + def.key + '"'
            + (tip ? ' data-tip="' + esc(tip) + '"' : '')
            + ' aria-pressed="' + (active ? 'true' : 'false') + '">'
            + esc(label)
            + (count > 0 ? '<span class="count">' + count + '</span>' : '')
            + '</button>';
    }).join('');

    container.querySelectorAll('.filter-chip[data-filter]').forEach((button) => {
        button.addEventListener('click', () => toggleFilter(button.dataset.filter));
    });
}

/* --- rendering --- */

// Host as plain text, e.g. "VS Code › CLI" for a terminal-driven session
// inside VS Code. "CLI" is a proper name, not translated.
function hostText(session) {
    let text = session.host || '';
    if (session.via_cli) {
        text = text ? text + ' › CLI' : 'CLI';
    }
    if (session.kind && session.kind !== 'interactive') {
        const kindLabel = state.labels['kind_' + session.kind] || session.kind;
        text = text ? text + ' · ' + kindLabel : kindLabel;
    }
    return text;
}

// A transcript UTC timestamp as a local date-time with seconds, e.g. for the
// model-switch history. Falls back to the raw value if it will not parse.
function fmtDateTime(iso) {
    const date = new Date(iso);
    if (isNaN(date.getTime())) {
        return String(iso == null ? '' : iso);
    }
    return date.toLocaleString(undefined, {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
}

// The model column: the current model (left), plus a "+N" badge (right, like
// the mode chip) when the session switched models. They share one cell that
// spreads them apart with space-between, so the name stays left-aligned and the
// badge right-aligned across rows. Hovering the badge lists the switch timeline
// oldest first - one line per model run with the time it began, so a model
// returned to appears again and the last line is the current model.
function modelCellHtml(session) {
    const name = '<span class="model-name">' + esc(session.model || '') + '</span>';
    if (!session.model_switched) {
        return name;
    }
    const history = session.model_history || [];
    const lines = history.map((entry) => fmtDateTime(entry.time) + '  ' + entry.label);
    return name + '<span class="model-more" data-tip="' + esc(lines.join('\n')) + '">+' + (history.length - 1) + '</span>';
}

function nameCellHtml(session) {
    const labels = state.labels;
    let html = '<span class="name">' + esc(session.name) + '</span>';

    if (session.mode) {
        html += '<span class="mode-chip">' + esc(session.mode) + '</span>';
    }

    // Running-subagent badge with a tooltip listing what each one is doing.
    if (session.subagents_running > 0) {
        const lines = [fmt(labels.subagents_running, { count: session.subagents_running })];
        (session.subagents_labels || []).forEach((label) => lines.push('• ' + label));
        if (session.subagents_done > 0) {
            lines.push(fmt(labels.subagents_finished, { count: session.subagents_done }));
        }
        html += '<span class="agents-badge" data-tip="' + esc(lines.join('\n')) + '">⚡ ' + session.subagents_running + '</span>';
    }

    // Background OS processes the session is running (e.g. a watched build).
    if (session.processes > 0) {
        const lines = [fmt(labels.processes_running, { count: session.processes })];
        (session.process_names || []).forEach((name) => lines.push('• ' + name));
        html += '<span class="proc-badge" data-tip="' + esc(lines.join('\n')) + '">⚙ ' + session.processes + '</span>';
    }

    return html;
}

function createRow() {
    const row = document.createElement('div');
    row.className = 'row';
    // No status-text column: the dot carries the status (label on hover),
    // and the filter chips in the top bar teach the colors.
    row.innerHTML = '<span class="dot"></span>'
        + '<div class="main-cell">'
        +     '<span class="name-cell"><span class="name"></span></span>'
        +     '<span class="usage-cell">'
        +         '<span class="usage-detail-wrap"><span class="usage-detail"></span></span>'
        +         '<span class="usage-compact"></span>'
        +     '</span>'
        + '</div>'
        + '<span class="model-cell"></span>'
        + '<span class="host-cell"></span>'
        + '<span class="age"></span>'
        + '<button class="row-menu-btn" type="button">&#8943;</button>';

    bindUsageHover(row.querySelector('.usage-cell'));
    return row;
}

// Inline-expand the usage figure into its full breakdown on a deliberate hover
// (>500ms), then collapse on leave. Bound once per row node in createRow -
// reconciliation reuses the node, so this never re-binds or stacks listeners.
function bindUsageHover(cell) {
    let timer = null;
    cell.addEventListener('mouseenter', () => {
        clearTimeout(timer);
        timer = setTimeout(() => cell.classList.add('open'), 500);
    });
    cell.addEventListener('mouseleave', () => {
        clearTimeout(timer);
        cell.classList.remove('open');
    });
}

function updateRow(row, session, projectName) {
    const labels = state.labels;

    row.className = 'row status-' + session.status + (session.is_history ? ' is-history' : '');
    row.dataset.project = projectName || '';
    row.dataset.session = session.session_id || '';
    row.dataset.title = session.title || '';
    // A history session has no live process, so it is not a focus target (no
    // data-pid); the click-to-focus handler keys on .row[data-pid].
    if (session.is_history) {
        delete row.dataset.pid;
    } else {
        row.dataset.pid = Number(session.pid);
    }
    if (session.vscode_deeplink) {
        row.dataset.deeplink = '1';
    } else {
        delete row.dataset.deeplink;
    }

    const age = ageLabel(session);

    // The dot is now the primary status signal, so it names the status itself.
    const dot = row.querySelector('.dot');
    if (session.status_label) {
        dot.dataset.tip = session.status_label;
    } else {
        delete dot.dataset.tip;
    }

    row.querySelector('.name-cell').innerHTML = nameCellHtml(session);
    row.querySelector('.usage-compact').textContent = session.usage_compact || '';
    row.querySelector('.usage-detail').textContent = session.usage_detail || '';
    row.querySelector('.model-cell').innerHTML = modelCellHtml(session);
    row.querySelector('.host-cell').textContent = hostText(session);

    const ageEl = row.querySelector('.age');
    ageEl.textContent = age;
    if (session.age_seconds != null) {
        ageEl.dataset.age = session.age_seconds;
    } else {
        delete ageEl.dataset.age;
    }

    const menuBtn = row.querySelector('.row-menu-btn');
    menuBtn.dataset.session = session.session_id || '';
    menuBtn.dataset.cwd = session.cwd || '';
    // Only a history row (a past, non-live session with no registry record) may
    // be deleted; the menu adds its delete item off this flag.
    if (session.is_history) {
        menuBtn.dataset.history = '1';
    } else {
        delete menuBtn.dataset.history;
    }
    menuBtn.setAttribute('aria-label', labels.row_menu || 'More actions');
}

function topStatus(sessions) {
    let top = null;
    for (const session of sessions) {
        if (top === null || (logic.STATUS_ORDER[session.status] ?? 99) < (logic.STATUS_ORDER[top.status] ?? 99)) {
            top = session;
        }
    }
    return top;
}

function createPanel() {
    const section = document.createElement('section');
    section.className = 'panel';
    section.innerHTML = '<div class="panel-head">'
        + '<h2></h2><span class="panel-path"><span class="path-open"></span></span><span class="head-status"></span>'
        + '<span class="panel-count"></span><span class="chevron"></span>'
        + '</div><div class="rows"></div>';
    return section;
}

function updatePanel(section, project) {
    const collapsed = state.collapsed.has(project.cwd);
    const anyNeeds = project.sessions.some((session) => session.needs_attention);

    section.classList.toggle('needs', anyNeeds);
    section.classList.toggle('collapsed', collapsed);

    const head = section.querySelector('.panel-head');
    head.dataset.cwd = project.cwd;
    section.querySelector('h2').textContent = project.name;
    section.querySelector('.panel-count').textContent = project.sessions.length;

    // The path text is its own click target (opens the folder in Explorer);
    // clicking elsewhere in the header still toggles the panel.
    const pathOpen = section.querySelector('.path-open');
    pathOpen.textContent = project.cwd;
    pathOpen.dataset.tip = state.labels.open_in_explorer || 'Open in Explorer';

    const headStatus = section.querySelector('.head-status');
    if (collapsed) {
        const top = topStatus(project.sessions);
        headStatus.className = 'head-status' + (top ? ' status-' + top.status : '');
        headStatus.textContent = top ? top.status_label : '';
    } else {
        headStatus.className = 'head-status';
        headStatus.textContent = '';
    }

    const rows = section.querySelector('.rows');
    if (collapsed) {
        rows.replaceChildren();
    } else {
        reconcile(rows, project.sessions, rowKey, createRow, (el, session) => updateRow(el, session, project.name));
    }
}

// One registry file per process, so the pid is a row's stable, unique identity.
// The session id alone is not unique: the same session open in two places - its
// VS Code window and a `claude --resume` in a terminal - yields two live records
// that share a session id but are distinct processes. Keying on the id alone
// would collide, and duplicate keys break the reconcile pass below (an orphaned
// node is never removed, so the list would grow by one per render without
// bound). Pairing the id with the pid keeps every record's key distinct.
function rowKey(session) {
    return (session.session_id || 'nosid') + '#' + session.pid;
}

// Keyed reconciliation: reuse existing child nodes (matched by data-key),
// create only new ones, remove departed ones, then order to match `items`.
// Reusing nodes is what lets an open row menu and the scroll position survive
// a refresh, and avoids the flicker of a full innerHTML rebuild.
function reconcile(container, items, keyOf, create, update) {
    const existing = new Map();
    for (const child of Array.from(container.children)) {
        if (child.dataset && child.dataset.key != null) {
            existing.set(child.dataset.key, child);
        }
    }

    const ordered = [];
    for (const item of items) {
        const key = String(keyOf(item));
        let el = existing.get(key);
        if (el) {
            existing.delete(key);
        } else {
            el = create(item);
            el.dataset.key = key;
        }
        update(el, item);
        ordered.push(el);
    }

    existing.forEach((el) => el.remove());

    ordered.forEach((el, index) => {
        if (container.children[index] !== el) {
            container.insertBefore(el, container.children[index] || null);
        }
    });
}

function setHero(blocked) {
    const links = blocked.map(({ session, projectName }) =>
        '<button class="hero-link" data-pid="' + Number(session.pid) + '"'
        + ' data-project="' + esc(projectName) + '"'
        + ' data-session="' + esc(session.session_id || '') + '"'
        + ' data-title="' + esc(session.title || '') + '"'
        + (session.vscode_deeplink ? ' data-deeplink="1"' : '')
        + ' data-tip="' + esc(session.status_label) + '">'
        + esc(session.name) + '</button>'
    ).join('');

    heroSlot.innerHTML = '<div class="hero"><span class="hero-icon">&#128276;</span>'
        + '<span class="hero-text">' + esc(fmt(state.labels.feedback_needed, { count: blocked.length })) + '</span>'
        + '<span class="hero-links">' + links + '</span></div>';
}

function persistCollapsed() {
    try {
        localStorage.setItem('amc-collapsed', JSON.stringify([...state.collapsed]));
    } catch (e) { /* storage unavailable */ }
}

function toggleCollapse(cwd) {
    if (!cwd) {
        return;
    }
    if (state.collapsed.has(cwd)) {
        state.collapsed.delete(cwd);
    } else {
        state.collapsed.add(cwd);
    }
    persistCollapsed();
    if (state.last) {
        render(state.last);
    }
}

function openPath(cwd) {
    if (!cwd) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.open_path === 'function') {
        bridge.open_path(cwd);
    }
}

function focusSession(el) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.focus_session === 'function') {
        bridge.focus_session(
            Number(el.dataset.pid),
            el.dataset.project || '',
            el.dataset.session || '',
            el.dataset.deeplink === '1',
            el.dataset.title || ''
        );
    }
}

// One delegated handler for the whole content area, so reconciled rows never
// need per-node listeners rebound on every render.
function onContentClick(event) {
    const menuBtn = event.target.closest('.row-menu-btn');
    if (menuBtn) {
        event.stopPropagation();
        const sessionId = menuBtn.dataset.session || '';
        const cwd = menuBtn.dataset.cwd || '';
        const items = [{ key: 'copy-id', label: state.labels.copy_session_id }];
        if (menuBtn.dataset.history === '1') {
            items.push({ key: 'delete', label: state.labels.delete_session, danger: true });
        }
        openMenu(menuBtn, items, (key) => {
            if (key === 'copy-id') {
                copyToClipboard(sessionId);
            } else if (key === 'delete') {
                confirmDeleteSession(sessionId, cwd);
            }
        }, { type: 'row', sessionId });
        return;
    }

    // Checked before the panel-head handler below: the path sits inside the
    // head, so it must claim its own click (open the folder) before the head's
    // collapse toggle can fire.
    const pathOpen = event.target.closest('.path-open');
    if (pathOpen) {
        event.stopPropagation();
        const pathHead = pathOpen.closest('.panel-head');
        openPath(pathHead ? pathHead.dataset.cwd : '');
        return;
    }

    const head = event.target.closest('.panel-head');
    if (head) {
        toggleCollapse(head.dataset.cwd);
        return;
    }

    // The hover-only info pills (the model "(+N)" history, the running-subagent
    // and background-process badges) are tooltip triggers with a help cursor, not
    // navigation targets - a click on one must not fall through to focusing the
    // session's window.
    if (event.target.closest('.model-more, .agents-badge, .proc-badge')) {
        return;
    }

    const focusEl = event.target.closest('.row[data-pid], .hero-link[data-pid]');
    if (focusEl) {
        focusSession(focusEl);
    }
}

function emptyBlock(message) {
    return '<div class="empty">' + esc(message || '') + '</div>';
}

// Column variable -> cell selector. After each render the widest cell per
// column (across ALL panels) is measured and written to the CSS variable, so
// every row shares identical column widths without wasting space.
const COLUMN_CELLS = [
    ['--col-model', '.row .model-cell'],
    ['--col-host', '.row .host-cell'],
];

function alignColumns() {
    const rootStyle = document.documentElement.style;

    // Let every cell take its natural width, measure, then lock the maximum.
    for (const [variable] of COLUMN_CELLS) {
        rootStyle.setProperty(variable, 'max-content');
    }

    const widths = COLUMN_CELLS.map(([, selector]) => {
        let max = 0;
        document.querySelectorAll(selector).forEach((cell) => {
            max = Math.max(max, cell.offsetWidth);
        });
        return max;
    });

    COLUMN_CELLS.forEach(([variable], index) => {
        rootStyle.setProperty(variable, widths[index] ? Math.ceil(widths[index]) + 'px' : 'max-content');
    });
}

// Stable shell created once; reconciliation happens inside the panels slot.
let heroSlot = null;
let panelsSlot = null;
let stateSlot = null;

function ensureShell() {
    if (panelsSlot && panelsSlot.isConnected) {
        return;
    }
    const content = document.getElementById('content');
    content.innerHTML = '<div class="hero-slot"></div><div class="panels-slot"></div><div class="state-slot"></div>';
    heroSlot = content.querySelector('.hero-slot');
    panelsSlot = content.querySelector('.panels-slot');
    stateSlot = content.querySelector('.state-slot');
}

function render(snapshot) {
    state.last = snapshot;

    const prices = logic.resolvePrices(state.pricing, todayIso());

    // Fold past sessions in only while the history chip is active and they have
    // finished loading; groupProjects then places them under their own project
    // panels, marked (and, in updateRow, styled) as history rows.
    const historyActive = state.filters.has('history');
    let rawSessions = snapshot.sessions || [];
    if (historyActive && Array.isArray(state.history)) {
        rawSessions = rawSessions.concat(state.history);
    }
    const loadingNote = (historyActive && state.historyLoading) ? state.labels.history_loading : '';

    const projects = logic.groupProjects(rawSessions, state.labels, prices);
    const counts = countByFilter(projects);
    renderFilters(counts);

    ensureShell();

    if (counts.all === 0) {
        heroSlot.replaceChildren();
        panelsSlot.replaceChildren();
        stateSlot.innerHTML = emptyBlock(loadingNote || state.labels.empty_state);
        syncOpenMenu();
        return;
    }

    // The banner shows only sessions genuinely blocked on a dialog (question,
    // plan review, permission) - the ones that need feedback to continue.
    const blocked = [];
    for (const project of projects) {
        for (const session of project.sessions) {
            if (session.status === 'awaiting_permission') {
                blocked.push({ session, projectName: project.name });
            }
        }
    }
    if (blocked.length > 0 && state.filters.has('needs')) {
        setHero(blocked);
    } else {
        heroSlot.replaceChildren();
    }

    const visible = [];
    for (const project of projects) {
        const sessions = sortSessions(project.sessions.filter(matchesFilter));
        if (sessions.length === 0) {
            continue;
        }
        visible.push({ cwd: project.cwd, name: project.name, sessions });
    }

    const ordered = logic.sortProjects(visible, state.priorityOrder);

    reconcile(panelsSlot, ordered, (project) => project.cwd, createPanel, updatePanel);
    if (loadingNote) {
        stateSlot.innerHTML = emptyBlock(loadingNote);
    } else {
        stateSlot.innerHTML = visible.length === 0 ? emptyBlock(state.labels.empty_filter || state.labels.empty_state) : '';
    }

    alignColumns();
    syncOpenMenu();
}

function renderLoading() {
    document.getElementById('content').innerHTML =
        '<div class="loading">'
        + '<div class="skeleton-panel"></div>'
        + '<div class="skeleton-panel short"></div>'
        + '<div class="skeleton-panel short"></div>'
        + '</div>';
}

function tickAges() {
    if (!state.last || state.receivedAt == null) {
        return;
    }

    const elapsed = Math.floor((Date.now() - state.receivedAt) / 1000);
    if (elapsed < 1) {
        return;
    }

    document.querySelectorAll('.age[data-age]').forEach((el) => {
        el.textContent = logic.formatAge(Number(el.dataset.age) + elapsed, state.labels);
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

    try {
        const snapshot = await callSnapshot();
        state.receivedAt = Date.now();
        render(snapshot);
    } catch (err) {
        reportUiError(err);
    }
}

async function boot() {
    if (state.booted) {
        return;
    }
    state.booted = true;

    try {
        state.filters = loadFilters();
        state.sort = localStorage.getItem('amc-sort') || 'activity';
        state.sortDir = localStorage.getItem('amc-sort-dir') === 'desc' ? 'desc' : 'asc';
        state.priorityOrder = localStorage.getItem('amc-priority-order') !== '0';
        state.collapsed = new Set(JSON.parse(localStorage.getItem('amc-collapsed') || '[]'));
    } catch (e) {
        state.filters = new Set(FILTER_KEYS);
        state.sort = 'activity';
        state.sortDir = 'asc';
        state.priorityOrder = true;
        state.collapsed = new Set();
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
        initSortControls();
    } catch (err) {
        reportUiError(err);
    }
    try {
        initPriorityToggle();
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
