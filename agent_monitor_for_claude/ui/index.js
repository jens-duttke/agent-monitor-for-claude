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
            // Contain an async rejection inline (not via logic.settleCall, which
            // may not have loaded): a rejected bridge.log would otherwise re-enter
            // unhandledrejection -> reportUiError and loop.
            const pending = bridge.log(message);
            if (pending && typeof pending.then === 'function') {
                pending.then(undefined, () => {});
            }
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
    // Free-text search over the session CONTENT (transcript text), in-session
    // only (deliberately not persisted, so a restart never hides everything
    // behind a stale query). The match is computed by the Python bridge, which
    // reads the transcripts and returns only the matching session ids - no
    // content ever reaches the UI. `searchMatches` holds that id set; null means
    // no content filter is active yet.
    search: '',
    searchMatches: null,
    searchLoading: false,
    searchProcessed: 0,
    searchTotal: 0,
    searchSeq: 0,
    searchTimer: null,
    // Editor-style search options (persisted), plus whether the current query is
    // an invalid regular expression (turns the box red).
    searchMatchCase: false,
    searchWholeWord: false,
    searchRegex: false,
    searchError: false,
    sort: 'activity',
    sortDir: 'asc',
    priorityOrder: true,
    collapsed: new Set(),
    last: null,
    receivedAt: null,
    // Epoch (ms) when the history list was fetched. History age_seconds is frozen
    // at that moment, so history rows must age from here, not from receivedAt (the
    // latest live-snapshot poll), or their age would freeze.
    historyReceivedAt: null,
    fingerprint: null,
    checking: false,
    // Monotonic token so an older, slower snapshot cannot render over a newer
    // one when the interval poll and a fingerprint-triggered tick overlap.
    snapshotGen: 0,
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
    search_placeholder: 'Search sessions',
    search_loading: 'Searching sessions…',
    search_match_case: 'Match case',
    search_whole_word: 'Match whole word',
    search_regex: 'Use regular expression',
    search_regex_invalid: 'Invalid regular expression',
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
    subagents_workflow: 'Workflow: {done}/{total} agents',
    processes_running: 'Background processes: {count}',
    proc_open_hint: 'Show CPU, memory and task output',
    proc_close: 'Close',
    proc_processes_title: 'Processes',
    proc_tasks_title: 'Background tasks',
    proc_col_process: 'Process',
    proc_col_cpu: 'CPU',
    proc_col_memory: 'Memory',
    proc_col_uptime: 'Uptime',
    proc_none: 'No background processes.',
    proc_wsl_vm: 'WSL2 VM (all distributions)',
    proc_wsl_vm_tip: 'The whole WSL2 virtual machine, shared across all distributions and sessions - not this session alone. WSL runs Linux processes inside this VM, so they cannot be listed individually.',
    proc_tasks_none: 'No background-task output - only run_in_background tasks write a live log.',
    task_label: 'Task {id}',
    task_active: 'active',
    task_last_output: 'last output {age} ago',
    task_output_loading: 'Loading output…',
    task_output_empty: '(no output yet)',
    tasks_more: '+{count} older',
    row_menu: 'More actions',
    copy_session_id: 'Copy session ID',
    copied: 'Copied to clipboard',
    open_in_explorer: 'Open in Explorer',
    open_scratchpad: 'Open scratchpad',
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
// for it. Derived in logic.js so the "never a fallback to all chips" rule is
// covered by a test.
const DEFAULT_FILTER_KEYS = logic.defaultFilterKeys(FILTER_DEFS);

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
    closeProcPanel();
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
        // The full row key (session_id + pid), not the session id alone: two rows
        // can share a session id (a live window plus a --resume terminal, or a
        // live+history duplicate), and re-anchoring on the id would jump to the
        // wrong one after rows reorder.
        rowKey: context.rowKey || null,
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
        anchor = findRowMenuButton(menu.rowKey);
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

function findRowMenuButton(rowKey) {
    if (!rowKey) {
        return null;
    }
    const rows = document.querySelectorAll('.row');
    for (const row of rows) {
        if (row.dataset.key === rowKey) {
            return row.querySelector('.row-menu-btn');
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

/* --- process panel ---

   Clicking the ⚙ background-process badge opens a panel anchored under it, with a
   close button (not a hover tooltip, so it survives while you read and expand
   rows). Two sections: a live per-process table (CPU / memory / uptime, polled
   once a second) and the session's background tasks. A task row expands to a
   mono-space console that tails that task's live output file once a second while
   it is open. This is the one place the UI shows process output text; the output
   is fetched on demand (read_task_output bridge), never on the snapshot poll, and
   only while a row is expanded. */
const PROC_POLL_MS = 1000;
const TASK_LIST_REFRESH_TICKS = 3;   // re-list tasks every 3rd stats tick, not every tick
const TASK_ACTIVE_SECONDS = 5;       // "active" when its output changed this recently
const TASK_AGE_GRACE_SECONDS = 60;   // keep tasks up to this much older than the oldest process

let procPanel = null;

function closeProcPanel() {
    const panel = procPanel;
    if (!panel) {
        return;
    }
    procPanel = null;
    clearTimeout(panel.timer);
    document.removeEventListener('click', panel.onDocClick, true);
    document.removeEventListener('keydown', panel.onKey, true);
    document.removeEventListener('scroll', panel.onViewportChange, true);
    window.removeEventListener('resize', panel.onViewportChange);
    if (panel.anchorEl) {
        panel.anchorEl.classList.remove('proc-badge-open');
    }
    panel.el.remove();
}

// Clicking the badge that already owns the open panel toggles it shut.
function toggleProcPanel(anchor) {
    const sameAnchor = procPanel && procPanel.anchorEl === anchor;
    closeProcPanel();
    if (sameAnchor) {
        return;
    }
    closeMenu();
    openProcPanel(anchor);
}

function openProcPanel(anchor) {
    const row = anchor.closest('.row');
    const el = document.createElement('div');
    el.className = 'proc-panel';
    el.setAttribute('role', 'dialog');
    el.innerHTML = ''
        + '<div class="proc-panel-head">'
        +     '<span class="proc-panel-title">'
        +         '<span class="proc-panel-count"></span>'
        +         '<span class="proc-panel-session"></span>'
        +     '</span>'
        +     '<button type="button" class="proc-panel-close" aria-label=""></button>'
        + '</div>'
        + '<div class="proc-panel-body">'
        +     '<section class="proc-section proc-section-processes">'
        +         '<div class="proc-section-title"></div>'
        +         '<table class="proc-table">'
        +             '<thead><tr>'
        +                 '<th class="proc-name"></th><th class="proc-num"></th>'
        +                 '<th class="proc-num"></th><th class="proc-num"></th>'
        +             '</tr></thead>'
        +             '<tbody></tbody>'
        +         '</table>'
        +     '</section>'
        +     '<section class="proc-section proc-section-tasks">'
        +         '<div class="proc-section-title"></div>'
        +         '<div class="proc-task-list"></div>'
        +         '<div class="proc-task-note" hidden></div>'
        +     '</section>'
        + '</div>';

    const labels = state.labels;
    el.querySelector('.proc-panel-close').setAttribute('aria-label', labels.proc_close || 'Close');
    el.querySelector('.proc-panel-close').textContent = '×';
    el.querySelector('.proc-section-processes .proc-section-title').textContent = labels.proc_processes_title || '';
    el.querySelector('.proc-section-tasks .proc-section-title').textContent = labels.proc_tasks_title || '';
    const headCells = el.querySelectorAll('.proc-table thead th');
    headCells[0].textContent = labels.proc_col_process || '';
    headCells[1].textContent = labels.proc_col_cpu || '';
    headCells[2].textContent = labels.proc_col_memory || '';
    headCells[3].textContent = labels.proc_col_uptime || '';

    // Which session this panel belongs to - read from the row's name so the
    // header still identifies it once the panel covers the row. Stable for the
    // panel's life (the session identity does not change), so set once.
    const nameEl = row ? row.querySelector('.name') : null;
    const sessionName = nameEl ? nameEl.textContent.trim() : '';
    if (sessionName) {
        // The gap before the dash comes from CSS margin, not a leading space -
        // a flex item trims its leading whitespace, which glued "6" to "-".
        el.querySelector('.proc-panel-session').textContent = '· ' + sessionName;
    }

    document.body.appendChild(el);
    anchor.classList.add('proc-badge-open');

    const panel = {
        el,
        anchorEl: anchor,
        rowKey: row ? row.dataset.key : null,
        pid: Number(anchor.dataset.procPid),
        sessionId: anchor.dataset.procSession || '',
        cwd: anchor.dataset.procCwd || '',
        expanded: new Set(),
        tick: 0,
        timer: null,
    };
    procPanel = panel;

    el.querySelector('.proc-panel-close').addEventListener('click', (event) => {
        event.stopPropagation();
        closeProcPanel();
    });

    panel.onDocClick = (event) => {
        if (!el.contains(event.target) && !anchor.contains(event.target) && event.target !== anchor) {
            closeProcPanel();
        }
    };
    panel.onKey = (event) => {
        if (event.key === 'Escape') {
            closeProcPanel();
        }
    };
    // Follow the anchor on scroll/resize (a background scroll moves the row); a
    // scroll inside the panel's own consoles just repositions to the same spot.
    panel.onViewportChange = () => {
        if (panel.anchorEl && panel.anchorEl.isConnected) {
            positionProcPanel(el, panel.anchorEl);
        }
    };

    // Defer the outside-click listener so the opening click does not close it.
    setTimeout(() => {
        if (procPanel === panel) {
            document.addEventListener('click', panel.onDocClick, true);
        }
    }, 0);
    document.addEventListener('keydown', panel.onKey, true);
    document.addEventListener('scroll', panel.onViewportChange, true);
    window.addEventListener('resize', panel.onViewportChange);

    positionProcPanel(el, anchor);
    procPanelLoop(panel);
}

// Self-scheduling poll so a slow tick never overlaps the next. Fills the panel
// immediately, then again every PROC_POLL_MS while it stays open.
async function procPanelLoop(panel) {
    if (procPanel !== panel) {
        return;
    }
    await refreshProcPanel(panel);
    if (procPanel !== panel) {
        return;
    }
    panel.timer = setTimeout(() => procPanelLoop(panel), PROC_POLL_MS);
}

async function refreshProcPanel(panel) {
    panel.tick += 1;

    const stats = await callProcessStats(panel.pid);
    if (procPanel !== panel) {
        return;
    }
    renderProcTable(panel, stats);
    panel.maxProcUptime = maxProcessUptime(stats);

    // The task list changes rarely, so re-list it only occasionally; the output
    // of expanded rows is what needs the per-second refresh.
    if (panel.tick === 1 || panel.tick % TASK_LIST_REFRESH_TICKS === 1) {
        // A task last written before the oldest running process started belongs
        // to an earlier run and is irrelevant, so it is dropped (small grace to
        // keep a just-started task that has not written yet).
        const cutoff = panel.maxProcUptime > 0 ? panel.maxProcUptime + TASK_AGE_GRACE_SECONDS : 0;
        const result = await callTasks(panel.sessionId, panel.cwd, cutoff);
        if (procPanel !== panel) {
            return;
        }
        renderTaskList(panel, result);
    }

    for (const taskId of Array.from(panel.expanded)) {
        const text = await callTaskOutput(panel.sessionId, panel.cwd, taskId);
        if (procPanel !== panel) {
            return;
        }
        setTaskOutput(panel, taskId, text);
    }

    positionProcPanel(panel.el, panel.anchorEl);
}

function renderProcTable(panel, stats) {
    const labels = state.labels;
    // The WSL2-VM row is context, not one of the session's processes, so the
    // count reflects only the real descendant processes (matching the badge).
    const processCount = stats.filter((stat) => stat.kind !== 'wsl_vm').length;
    const count = panel.el.querySelector('.proc-panel-count');
    if (count) {
        count.textContent = fmt(labels.processes_running, { count: processCount });
    }
    const tbody = panel.el.querySelector('.proc-table tbody');
    if (!tbody) {
        return;
    }
    if (stats.length === 0) {
        tbody.innerHTML = '<tr class="proc-empty"><td colspan="4"></td></tr>';
        tbody.querySelector('td').textContent = labels.proc_none || '';
        return;
    }
    let rows = '';
    for (const stat of stats) {
        if (stat.kind === 'wsl_vm') {
            rows += '<tr class="proc-vm">'
                + '<td class="proc-name" data-tip="' + esc(labels.proc_wsl_vm_tip || '') + '">' + esc(labels.proc_wsl_vm || 'WSL2 VM') + '</td>'
                + '<td class="proc-num">' + esc(formatCpu(stat.cpu)) + '</td>'
                + '<td class="proc-num">' + esc(formatBytes(stat.rss)) + '</td>'
                + '<td class="proc-num">-</td>'
                + '</tr>';
        } else {
            rows += '<tr>'
                + '<td class="proc-name">' + esc(stat.name) + '</td>'
                + '<td class="proc-num">' + esc(formatCpu(stat.cpu)) + '</td>'
                + '<td class="proc-num">' + esc(formatBytes(stat.rss)) + '</td>'
                + '<td class="proc-num">' + esc(formatDurationShort(stat.uptime)) + '</td>'
                + '</tr>';
        }
    }
    tbody.innerHTML = rows;
}

// Keyed reconcile (not innerHTML) so an expanded console and its scroll position
// survive a re-list.
function renderTaskList(panel, result) {
    const labels = state.labels;
    const listEl = panel.el.querySelector('.proc-task-list');
    const section = panel.el.querySelector('.proc-section-tasks');
    const noteEl = panel.el.querySelector('.proc-task-note');
    const tasks = (result && result.tasks) || [];
    const total = (result && result.total) || tasks.length;

    // Keep the section visible even with no tasks, with an explanatory note -
    // this is where a user looks for process output, so silence would just read
    // as "broken". Only run_in_background tasks write a live output file.
    let emptyNote = section.querySelector('.proc-task-empty');
    if (tasks.length === 0) {
        if (!emptyNote) {
            emptyNote = document.createElement('div');
            emptyNote.className = 'proc-task-empty';
            listEl.after(emptyNote);
        }
        emptyNote.textContent = labels.proc_tasks_none || '';
        emptyNote.hidden = false;
    } else if (emptyNote) {
        emptyNote.hidden = true;
    }

    const existing = {};
    Array.from(listEl.children).forEach((node) => {
        if (node.dataset.taskId) {
            existing[node.dataset.taskId] = node;
        }
    });

    const seen = new Set();
    let prev = null;
    for (const task of tasks) {
        seen.add(task.id);
        let node = existing[task.id];
        if (!node) {
            node = createTaskRow(panel, task);
        }
        updateTaskRow(node, task);
        const after = prev ? prev.nextSibling : listEl.firstChild;
        if (after !== node) {
            listEl.insertBefore(node, after);
        }
        prev = node;
    }

    Array.from(listEl.children).forEach((node) => {
        if (node.dataset.taskId && !seen.has(node.dataset.taskId)) {
            panel.expanded.delete(node.dataset.taskId);
            node.remove();
        }
    });

    const dropped = total - tasks.length;
    if (dropped > 0) {
        noteEl.hidden = false;
        noteEl.textContent = fmt(labels.tasks_more, { count: dropped });
    } else {
        noteEl.hidden = true;
    }
}

function createTaskRow(panel, task) {
    const row = document.createElement('div');
    row.className = 'proc-task';
    row.dataset.taskId = task.id;
    row.innerHTML = ''
        + '<button type="button" class="proc-task-head">'
        +     '<span class="proc-task-caret" aria-hidden="true">▸</span>'
        +     '<span class="proc-task-label"></span>'
        +     '<span class="proc-task-meta"></span>'
        + '</button>'
        + '<pre class="proc-task-output" hidden></pre>';
    row.querySelector('.proc-task-head').addEventListener('click', (event) => {
        event.stopPropagation();
        toggleTask(panel, row, task.id);
    });
    return row;
}

function updateTaskRow(row, task) {
    const labels = state.labels;
    const label = (task.label && task.label.trim()) ? task.label.trim() : fmt(labels.task_label, { id: task.id });
    row.querySelector('.proc-task-label').textContent = label;
    // The task age refreshes only every few seconds, so it is shown coarse
    // (hours/minutes) - unlike the per-second process uptime.
    const active = task.age <= TASK_ACTIVE_SECONDS;
    row.querySelector('.proc-task-meta').textContent =
        (active ? (labels.task_active || '') : fmt(labels.task_last_output, { age: formatDurationCoarse(task.age) }))
        + ' · ' + formatBytes(task.size);
    row.classList.toggle('is-active', active);
}

function toggleTask(panel, row, taskId) {
    const out = row.querySelector('.proc-task-output');
    if (panel.expanded.has(taskId)) {
        panel.expanded.delete(taskId);
        out.hidden = true;
        row.classList.remove('is-open');
    } else {
        panel.expanded.add(taskId);
        out.hidden = false;
        row.classList.add('is-open');
        out.textContent = state.labels.task_output_loading || '…';
        out.__rawText = null;
        callTaskOutput(panel.sessionId, panel.cwd, taskId).then((text) => {
            if (procPanel === panel && panel.expanded.has(taskId)) {
                setTaskOutput(panel, taskId, text);
            }
        });
    }
    positionProcPanel(panel.el, panel.anchorEl);
}

function setTaskOutput(panel, taskId, text) {
    if (!panel.expanded.has(taskId)) {
        return;
    }
    const listEl = panel.el.querySelector('.proc-task-list');
    const row = Array.from(listEl.children).find((node) => node.dataset.taskId === taskId);
    if (!row) {
        return;
    }
    const out = row.querySelector('.proc-task-output');
    if (!out || out.hidden) {
        return;
    }
    // Don't clobber a selection the user is making inside this console: skip the
    // live refresh while text here is selected, so it stays put and Ctrl+C can
    // copy it. The next tick (once nothing is selected) catches up.
    const selection = window.getSelection ? window.getSelection() : null;
    if (selection && !selection.isCollapsed && selection.rangeCount > 0
        && out.contains(selection.anchorNode) && out.contains(selection.focusNode)) {
        return;
    }
    const content = (text == null || text === '') ? (state.labels.task_output_empty || '') : text;
    // Compare against the raw text, since the rendered HTML (colored spans) is
    // no longer a faithful string to diff against.
    if (out.__rawText === content) {
        return;
    }
    // Follow the tail only when the user is already at the bottom, so scrolling
    // back to read earlier output is not yanked away on the next refresh.
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 4;
    out.__rawText = content;
    out.innerHTML = ansiToHtml(content);
    if (atBottom) {
        out.scrollTop = out.scrollHeight;
    }
}

function positionProcPanel(el, anchor) {
    const rect = anchor.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const width = el.offsetWidth;
    const height = el.offsetHeight;

    let left = Math.min(rect.left, window.innerWidth - width - margin);
    left = Math.max(margin, left);

    let top = rect.bottom + gap;
    if (top + height + margin > window.innerHeight) {
        const above = rect.top - height - gap;
        top = above >= margin ? above : Math.max(margin, window.innerHeight - height - margin);
    }

    el.style.left = Math.round(left) + 'px';
    el.style.top = Math.round(Math.max(margin, top)) + 'px';
}

// Re-anchor after a render (rows reorder but nodes are reused); close if the
// badge is gone (the session ended or its process count dropped to zero).
function syncProcPanel() {
    const panel = procPanel;
    if (!panel) {
        return;
    }
    const anchor = findProcBadge(panel.rowKey);
    if (!anchor) {
        closeProcPanel();
        return;
    }
    if (anchor !== panel.anchorEl) {
        panel.anchorEl.classList.remove('proc-badge-open');
        anchor.classList.add('proc-badge-open');
        panel.anchorEl = anchor;
    }
    positionProcPanel(panel.el, anchor);
}

function findProcBadge(rowKey) {
    if (!rowKey) {
        return null;
    }
    const rows = document.querySelectorAll('.row');
    for (const row of rows) {
        if (row.dataset.key === rowKey) {
            return row.querySelector('.proc-badge');
        }
    }
    return null;
}

/* Bridge calls for the panel. Each degrades to the browser-preview mocks (or an
   empty result) when there is no pywebview bridge, so ?mock still demos it. */
async function callProcessStats(pid) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_process_stats === 'function') {
        try {
            return (await bridge.get_process_stats(pid)) || [];
        } catch (e) {
            return [];
        }
    }
    return resolveMock(window.__MOCK_PROC_STATS__, pid) || [];
}

async function callTasks(sessionId, cwd, maxAge) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_tasks === 'function') {
        try {
            return (await bridge.get_tasks(sessionId, cwd, maxAge || 0)) || { tasks: [], total: 0 };
        } catch (e) {
            return { tasks: [], total: 0 };
        }
    }
    const tasks = resolveMock(window.__MOCK_TASKS__, sessionId) || [];
    return { tasks: tasks, total: tasks.length };
}

async function callTaskOutput(sessionId, cwd, taskId) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.read_task_output === 'function') {
        try {
            return await bridge.read_task_output(sessionId, cwd, taskId);
        } catch (e) {
            return null;
        }
    }
    return resolveMock(window.__MOCK_TASK_OUTPUT__, taskId);
}

// The longest a real (non-VM) descendant process has been running, or 0 when
// none reports an uptime. Used as the relevance cutoff for the task list.
function maxProcessUptime(stats) {
    let max = 0;
    for (const stat of stats) {
        if (stat.kind !== 'wsl_vm' && typeof stat.uptime === 'number' && stat.uptime > max) {
            max = stat.uptime;
        }
    }
    return max;
}

// ANSI SGR foreground codes -> themed CSS class. Backgrounds and other
// attributes are intentionally not mapped (kept simple and legible).
const ANSI_FG = {
    30: 'ansi-black', 31: 'ansi-red', 32: 'ansi-green', 33: 'ansi-yellow',
    34: 'ansi-blue', 35: 'ansi-magenta', 36: 'ansi-cyan', 37: 'ansi-white',
    90: 'ansi-bright-black', 91: 'ansi-bright-red', 92: 'ansi-bright-green', 93: 'ansi-bright-yellow',
    94: 'ansi-bright-blue', 95: 'ansi-bright-magenta', 96: 'ansi-bright-cyan', 97: 'ansi-bright-white',
};

// Render terminal output as safe HTML: escape every text run, turn ANSI SGR
// color codes into themed spans, and drop the other control sequences a
// non-emulating console cannot honor (cursor moves, line erases). Only fixed,
// known class names are emitted - never any part of the raw code - so nothing
// untrusted reaches an attribute.
function ansiToHtml(text) {
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
        html += classes.length ? '<span class="' + classes.join(' ') + '">' + escaped + '</span>' : escaped;
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

// A mock entry may be a value or a thunk (so a preview can jitter live).
function resolveMock(store, key) {
    if (!store) {
        return null;
    }
    const entry = store[key];
    return typeof entry === 'function' ? entry() : entry;
}

function formatCpu(cpu) {
    if (cpu == null) {
        return '…';
    }
    return (cpu < 10 ? cpu.toFixed(1) : String(Math.round(cpu))) + '%';
}

function formatBytes(bytes) {
    if (bytes == null) {
        return '…';
    }
    if (bytes < 1024) {
        return bytes + ' B';
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return (value < 10 ? value.toFixed(1) : String(Math.round(value))) + ' ' + units[unit];
}

// Always ends in seconds, since the panel ticks every second; larger units are
// zero-padded once a bigger one precedes them (e.g. "3h 07m 05s").
function formatDurationShort(seconds) {
    if (seconds == null) {
        return '…';
    }
    let total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const secs = total - minutes * 60;
    const pad = (value) => String(value).padStart(2, '0');
    if (days > 0) {
        return days + 'd ' + pad(hours) + 'h ' + pad(minutes) + 'm ' + pad(secs) + 's';
    }
    if (hours > 0) {
        return hours + 'h ' + pad(minutes) + 'm ' + pad(secs) + 's';
    }
    if (minutes > 0) {
        return minutes + 'm ' + pad(secs) + 's';
    }
    return secs + 's';
}

// Coarse form (no seconds) for values refreshed only every few seconds, so the
// text does not imply a precision it does not have.
function formatDurationCoarse(seconds) {
    if (seconds == null) {
        return '…';
    }
    let total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const pad = (value) => String(value).padStart(2, '0');
    if (days > 0) {
        return days + 'd ' + pad(hours) + 'h';
    }
    if (hours > 0) {
        return hours + 'h ' + pad(minutes) + 'm';
    }
    if (minutes > 0) {
        return minutes + 'm';
    }
    return '<1m';
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
            state.history = Array.isArray(window.__MOCK_HISTORY__) ? window.__MOCK_HISTORY__ : [];
            state.historyReceivedAt = Date.now();
            afterHistoryLoaded();
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
        state.historyReceivedAt = Date.now();
    } catch (e) {
        // A failed one-shot fetch must not cache []: that empty sentinel is what
        // a genuine empty result uses, so the loaded-guard would be satisfied
        // forever and never retry. Reset to null so toggling History off and on
        // (or the next invalidation) re-fetches.
        state.history = null;
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

// Content search. The query is matched against the transcript CONTENT, which
// only the Python bridge can read; it streams back matching session ids (and
// progress) via window.__amcSearchPush, and the UI shows them as they arrive.
// The box combines with the status chips (both must match). A query shorter than
// this is treated as no filter, so a single stray character never scans.
const SEARCH_MIN_CHARS = 2;

// Wait for a pause in typing before scanning, so a burst of keystrokes starts
// one search, not one per character.
const SEARCH_DEBOUNCE_MS = 300;

// The editor-style option toggles: element id -> the state flag it drives.
const SEARCH_OPT_BUTTONS = [
    ['opt-case', 'searchMatchCase', 'search_match_case'],
    ['opt-word', 'searchWholeWord', 'search_whole_word'],
    ['opt-regex', 'searchRegex', 'search_regex'],
];

function initSearch() {
    const input = document.getElementById('search-input');
    if (!input) {
        return;
    }

    // Receive streaming results pushed from the Python side. Registered here so
    // it exists before any search can start.
    window.__amcSearchPush = onSearchPush;

    input.value = state.search;

    input.addEventListener('input', () => {
        state.search = input.value;
        scheduleSearch();
    });

    input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && input.value) {
            event.stopPropagation();
            input.value = '';
            state.search = '';
            scheduleSearch();
        }
    });

    // Each toggle flips its option, is persisted, and re-runs the search at once
    // (a click is a discrete action, so no debounce).
    SEARCH_OPT_BUTTONS.forEach(([id, key]) => {
        const button = document.getElementById(id);
        if (!button) {
            return;
        }
        button.addEventListener('click', () => {
            state[key] = !state[key];
            persistSearchOptions();
            refreshSearchOptButtons();
            runSearch();
        });
    });

    refreshSearchOptButtons();
}

// Reflect the option flags on their buttons (active state + tooltip). Called on
// init, after each toggle, and once labels arrive (applyBootstrap).
function refreshSearchOptButtons() {
    SEARCH_OPT_BUTTONS.forEach(([id, key, tipKey]) => {
        const button = document.getElementById(id);
        if (!button) {
            return;
        }
        const on = !!state[key];
        button.classList.toggle('active', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
        const tip = state.labels[tipKey];
        if (tip) {
            button.dataset.tip = tip;
            button.setAttribute('aria-label', tip);
        }
    });
}

function persistSearchOptions() {
    try {
        localStorage.setItem('amc-search-opts', JSON.stringify({
            matchCase: state.searchMatchCase,
            wholeWord: state.searchWholeWord,
            regex: state.searchRegex,
        }));
    } catch (e) { /* storage unavailable */ }
}

function searchOptions() {
    return { match_case: state.searchMatchCase, whole_word: state.searchWholeWord, use_regex: state.searchRegex };
}

// Front-end regex validity check for instant red-box feedback. The backend
// (Python `re`) is authoritative and can still flag an error via its push, but
// this catches the common case without a round-trip.
function isValidRegex(pattern) {
    try {
        new RegExp(pattern);
        return true;
    } catch (e) {
        return false;
    }
}

// Toggle the invalid (red) state of the box, and give it a tooltip explaining
// why while invalid.
function updateSearchBox() {
    const box = document.querySelector('.search-box');
    if (box) {
        box.classList.toggle('invalid', !!state.searchError);
    }
    const input = document.getElementById('search-input');
    if (input) {
        if (state.searchError) {
            input.setAttribute('aria-invalid', 'true');
            input.dataset.tip = state.labels.search_regex_invalid || '';
        } else {
            input.removeAttribute('aria-invalid');
            delete input.dataset.tip;
        }
    }
}

function scheduleSearch() {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
}

// The search scope (in logic.searchScopeRefs) is exactly the sessions the active
// filter chips show - not every session - so Python reads only the transcripts
// the user can actually see. The delta rescan (includeHistory false) additionally
// skips dead sessions, which cannot gain a new match.
function collectSearchRefs(includeHistory) {
    return logic.searchScopeRefs(
        state.last ? state.last.sessions : [],
        state.history,
        state.filters,
        includeHistory
    );
}

function currentSessionRefs() {
    return collectSearchRefs(true);
}

// Start (or clear) a content search. Bumps the sequence id so any in-flight scan
// - and any late push from it - is superseded and ignored.
function runSearch() {
    // A direct caller (a filter chip or a search-option toggle) can run this
    // while a debounce timer from recent typing is still pending. Clear it, or it
    // fires afterward and restarts an identical search - dropping the matches
    // already streaming in and restarting the progress bar.
    clearTimeout(state.searchTimer);
    state.searchTimer = null;

    const query = state.search.trim();
    const active = query.length >= SEARCH_MIN_CHARS;
    const seq = ++state.searchSeq;
    const bridge = apiBridge();

    // Regex mode with an invalid pattern: flag the box red at once and do not
    // scan. (The backend re-validates too and can flag it via its push.)
    if (active && state.searchRegex && !isValidRegex(query)) {
        state.searchError = true;
        state.searchMatches = null;
        state.searchLoading = false;
        state.searchProcessed = 0;
        state.searchTotal = 0;
        // Cancel any in-flight backend scan (a prior valid pattern still running):
        // the local seq bump is invisible to Python, so without a fresh
        // start_search the earlier scan reads every remaining transcript to the end.
        if (bridge && typeof bridge.start_search === 'function') {
            logic.settleCall(() => bridge.start_search('', [], searchOptions(), seq));
        }
        updateSearchProgress();
        updateSearchBox();
        if (state.last) {
            render(state.last);
        }
        return;
    }
    state.searchError = false;
    updateSearchBox();

    if (!active) {
        state.searchMatches = null;
        state.searchLoading = false;
        state.searchProcessed = 0;
        state.searchTotal = 0;
        // Cancel a running backend search: the new (unused) seq invalidates it.
        if (bridge && typeof bridge.start_search === 'function') {
            logic.settleCall(() => bridge.start_search('', [], searchOptions(), seq));
        }
        updateSearchProgress();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    state.searchMatches = new Set();
    state.searchLoading = true;
    state.searchProcessed = 0;
    state.searchTotal = 0;
    updateSearchProgress();
    if (state.last) {
        render(state.last);
    }

    if (!bridge || typeof bridge.start_search !== 'function') {
        // Browser preview: no file access without the bridge, so fall back to a
        // synchronous client-side match over the mock records' visible fields.
        state.searchMatches = mockSearchMatches(query);
        state.searchLoading = false;
        updateSearchProgress();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    // Fire-and-forget: a Python-side rejection must not escape to the global
    // handler (which wipes the content area) or leave searchLoading stuck true.
    logic.settleCall(
        () => bridge.start_search(query, currentSessionRefs(), searchOptions(), seq),
        () => {
            state.searchLoading = false;
            updateSearchProgress();
            if (state.last) {
                render(state.last);
            }
        }
    );
}

// Refresh an active search when the snapshot changes, so newly-appearing matches
// show up on their own. Transcripts are append-only: an existing match never
// stops matching, and a NEW match can only appear in a live session that has
// grown. So the delta scope is just the chip-visible live sessions not already
// matched - cheap even with history on (dead history is never re-read, matched
// sessions are skipped). Runs silently (no progress bar): the new matches simply
// stream in and are ADDED to the set, so nothing already shown flickers away.
function rescanForNewMatches() {
    const query = state.search.trim();
    if (query.length < SEARCH_MIN_CHARS || state.searchError || state.searchLoading) {
        return;
    }
    if (!(state.searchMatches instanceof Set)) {
        return;
    }

    const bridge = apiBridge();
    if (!bridge || typeof bridge.start_search !== 'function') {
        return;
    }

    const refs = collectSearchRefs(false).filter((ref) => !state.searchMatches.has(ref.session_id));
    if (refs.length === 0) {
        return;
    }

    // A fresh seq so any late push from a prior scan is ignored; matches are
    // added to the existing set (never reset), so results only grow.
    const seq = ++state.searchSeq;
    // Fire-and-forget; a bridge hiccup (sync or async) is contained - the next
    // change retries - and never reaches the global handler.
    logic.settleCall(() => bridge.start_search(query, refs, searchOptions(), seq));
}

// One streaming update from the backend: {seq, processed, total, ids, done, error}.
// A stale seq (a newer search has started) is ignored. Pure progress ticks only
// move the bar; a re-render happens when new matches arrive or the scan ends.
function onSearchPush(payload) {
    if (!payload || payload.seq !== state.searchSeq) {
        return;
    }

    // The search could not complete: an invalid regular expression, or the
    // backend scan failed. Either way, do not present it as "no matches".
    if (payload.error) {
        state.searchError = true;
        state.searchLoading = false;
        state.searchMatches = null;
        updateSearchProgress();
        updateSearchBox();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    state.searchProcessed = payload.processed || 0;
    state.searchTotal = payload.total || 0;

    let changed = false;
    if (Array.isArray(payload.ids) && payload.ids.length) {
        if (!(state.searchMatches instanceof Set)) {
            state.searchMatches = new Set();
        }
        for (const id of payload.ids) {
            if (!state.searchMatches.has(id)) {
                state.searchMatches.add(id);
                changed = true;
            }
        }
    }
    if (payload.done) {
        state.searchLoading = false;
        changed = true;
    }

    updateSearchProgress();
    if (changed && state.last) {
        render(state.last);
    }
}

// A thin determinate progress bar under the toolbar while a scan runs; before
// the first update arrives (total unknown) it reads as indeterminate.
function updateSearchProgress() {
    const bar = document.getElementById('search-progress');
    if (!bar) {
        return;
    }

    bar.hidden = !state.searchLoading;
    const indeterminate = state.searchLoading && state.searchTotal === 0;
    bar.classList.toggle('indeterminate', indeterminate);

    const fill = bar.querySelector('.search-progress-fill');
    if (fill) {
        const pct = state.searchTotal > 0
            ? Math.min(100, Math.round((state.searchProcessed / state.searchTotal) * 100))
            : 0;
        fill.style.width = indeterminate ? '' : pct + '%';
    }
}

// Preview-only fallback (no bridge, so no transcript access): match the query
// against the mock records' visible fields, over the same in-view scope the real
// search uses. Never used in the packaged app.
function mockSearchMatches(query) {
    const needle = query.toLowerCase();
    const scope = new Set(currentSessionRefs().map((ref) => ref.session_id));
    const matches = new Set();
    const scan = (list) => {
        for (const raw of list || []) {
            if (!scope.has(raw.session_id)) {
                continue;
            }
            const hay = [raw.title, raw.short_name, raw.cwd, raw.model_id].filter(Boolean).join(' ').toLowerCase();
            if (hay.includes(needle)) {
                matches.add(raw.session_id);
            }
        }
    };
    scan(state.last ? state.last.sessions : []);
    if (Array.isArray(state.history)) {
        scan(state.history);
    }
    return matches;
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

    // Running-subagent badge with a tooltip listing what each one is doing. For a
    // background workflow the badge shows the run's total agent count - stable
    // across the fan-out phases where no single agent is momentarily running, so
    // it no longer flickers away - and the tooltip leads with that total.
    if (session.subagents_running > 0 || session.workflow_active) {
        const lines = [];
        if (session.workflow_active) {
            lines.push(fmt(labels.subagents_workflow, { done: session.workflow_done, total: session.workflow_total }));
        }
        if (session.subagents_running > 0) {
            lines.push(fmt(labels.subagents_running, { count: session.subagents_running }));
        }
        (session.subagents_labels || []).forEach((label) => lines.push('• ' + label));
        // "Recently finished" is the agent-file scan's recent-window count; for a
        // workflow the "{done}/{total}" line above already conveys progress, so
        // this line is only for the plain-subagent case (never the cumulative
        // workflow total mislabelled as "recently").
        if (!session.workflow_active && session.subagents_done > 0) {
            lines.push(fmt(labels.subagents_finished, { count: session.subagents_done }));
        }
        const badgeCount = session.workflow_active ? session.workflow_total : session.subagents_running;
        html += '<span class="agents-badge" data-tip="' + esc(lines.join('\n')) + '">⚡ ' + badgeCount + '</span>';
    }

    // Background OS processes the session is running (e.g. a watched build).
    // Clicking the badge opens the process panel (CPU/RAM/uptime plus the
    // session's background-task output); the pid/session/cwd it needs ride along
    // as data attributes. data-tip gives a hover hint about the click action.
    if (session.processes > 0) {
        html += '<span class="proc-badge"'
            + ' data-proc-pid="' + esc(session.pid) + '"'
            + ' data-proc-session="' + esc(session.session_id) + '"'
            + ' data-proc-cwd="' + esc(session.cwd) + '"'
            + ' data-tip="' + esc(labels.proc_open_hint || '') + '">⚙ ' + session.processes + '</span>';
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

    // A live row's age was captured at the latest snapshot (receivedAt); a
    // history row's at the one-time history fetch (historyReceivedAt). Storing
    // that capture epoch per row lets tickAges advance each age from the right
    // moment, so history ages keep growing instead of freezing at fetch time.
    const ageEl = row.querySelector('.age');
    if (session.age_seconds != null) {
        const ageAt = session.is_history ? state.historyReceivedAt : state.receivedAt;
        ageEl.dataset.age = session.age_seconds;
        ageEl.dataset.ageAt = ageAt != null ? ageAt : Date.now();
        ageEl.textContent = logic.formatAgeSince(session.age_seconds, Number(ageEl.dataset.ageAt), Date.now(), labels);
    } else {
        delete ageEl.dataset.age;
        delete ageEl.dataset.ageAt;
        ageEl.textContent = '';
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
        // Fire-and-forget: a Win32-side rejection must not wipe the content area.
        logic.settleCall(() => bridge.open_path(cwd));
    }
}

function focusSession(el) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.focus_session === 'function') {
        // Fire-and-forget: a Win32-side rejection must not wipe the content area.
        logic.settleCall(() => bridge.focus_session(
            Number(el.dataset.pid),
            el.dataset.project || '',
            el.dataset.session || '',
            el.dataset.deeplink === '1',
            el.dataset.title || ''
        ));
    }
}

// Build and open a row's action menu. The scratchpad entry is added only when
// that session actually has a scratchpad directory - checked on demand here (a
// quick bridge stat), never on the per-second poll.
async function openRowMenu(menuBtn) {
    const sessionId = menuBtn.dataset.session || '';
    const cwd = menuBtn.dataset.cwd || '';
    const rowEl = menuBtn.closest('.row');
    const menuRowKey = rowEl ? rowEl.dataset.key : '';

    const scratchpad = await scratchpadPath(sessionId, cwd);

    // The row could have been reconciled away while the check was in flight.
    if (!menuBtn.isConnected) {
        return;
    }

    const items = [{ key: 'copy-id', label: state.labels.copy_session_id }];
    if (scratchpad) {
        items.push({ key: 'scratchpad', label: state.labels.open_scratchpad });
    }
    if (menuBtn.dataset.history === '1') {
        items.push({ key: 'delete', label: state.labels.delete_session, danger: true });
    }

    openMenu(menuBtn, items, (key) => {
        if (key === 'copy-id') {
            copyToClipboard(sessionId);
        } else if (key === 'scratchpad') {
            openPath(scratchpad);
        } else if (key === 'delete') {
            confirmDeleteSession(sessionId, cwd);
        }
    }, { type: 'row', rowKey: menuRowKey });
}

// Return the session's scratchpad path when one exists, else '' (no entry).
async function scratchpadPath(sessionId, cwd) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.scratchpad_path === 'function') {
        try {
            return (await bridge.scratchpad_path(sessionId, cwd)) || '';
        } catch (e) {
            return '';
        }
    }
    const mock = window.__MOCK_SCRATCHPADS__;
    return (mock && mock[sessionId]) ? mock[sessionId] : '';
}

// One delegated handler for the whole content area, so reconciled rows never
// need per-node listeners rebound on every render.
function onContentClick(event) {
    const menuBtn = event.target.closest('.row-menu-btn');
    if (menuBtn) {
        event.stopPropagation();
        openRowMenu(menuBtn);
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

    // The background-process badge opens the process panel; its own click must
    // not fall through to focusing the session's window.
    const procBadge = event.target.closest('.proc-badge');
    if (procBadge) {
        event.stopPropagation();
        toggleProcPanel(procBadge);
        return;
    }

    // The hover-only info pills (the model "(+N)" history, the running-subagent
    // badge) are tooltip triggers with a help cursor, not navigation targets -
    // a click on one must not fall through to focusing the session's window.
    if (event.target.closest('.model-more, .agents-badge')) {
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
    const previous = state.last;
    state.last = snapshot;

    const prices = logic.resolvePrices(state.pricing, todayIso());

    // Fold past sessions in only while the history chip is active and they have
    // finished loading; groupProjects then places them under their own project
    // panels, marked (and, in updateRow, styled) as history rows.
    const historyActive = state.filters.has('history');
    let rawSessions = snapshot.sessions || [];
    if (historyActive && Array.isArray(state.history)) {
        // Drop a resumed past session's stale history row (it is live again, so
        // it is already in the snapshot) - otherwise it renders twice.
        rawSessions = rawSessions.concat(logic.pruneResumedHistory(state.history, snapshot.sessions || []));
        // A previously-live session that just left the snapshot (ended and its
        // registry record pruned) was excluded from the one-shot history fetch;
        // re-fetch so it can move into history instead of vanishing from both.
        if (previous && logic.historyNeedsRefresh(previous.sessions, snapshot.sessions)) {
            state.history = null;
            ensureHistoryLoaded();
        }
    }
    const loadingNote = (historyActive && state.historyLoading) ? state.labels.history_loading : '';

    const projects = logic.groupProjects(rawSessions, state.labels, prices);

    // The content search narrows the whole view at once - the chip counts, the
    // blocked banner, and the rows all reflect only sessions the backend matched
    // (matches stream in, so the set grows live). A query below the minimum, or
    // no query, matches everything. It combines with the status chips: a session
    // shows only when both its chip is on and its content matched.
    const searchActive = state.search.trim().length >= SEARCH_MIN_CHARS;
    const matchesSearch = (session) => logic.sessionMatchesSearch(session.session_id, searchActive, state.searchMatches);

    const counts = countByFilter(projects, matchesSearch);
    renderFilters(counts);

    ensureShell();

    // "No sessions at all" (or the history scan still loading) shows the empty
    // state; a query that merely matched nothing falls through to the per-filter
    // empty note below, so the search count is not mistaken for an idle machine.
    const hasAnySession = projects.some((project) => project.sessions.length > 0);
    if (!hasAnySession) {
        heroSlot.replaceChildren();
        panelsSlot.replaceChildren();
        stateSlot.innerHTML = emptyBlock(loadingNote || state.labels.empty_state);
        syncOpenMenu();
        syncProcPanel();
        return;
    }

    // The banner shows only sessions genuinely blocked on a dialog (question,
    // plan review, permission) that also match the search - the ones that need
    // feedback to continue.
    const blocked = [];
    for (const project of projects) {
        for (const session of project.sessions) {
            if (session.status === 'awaiting_permission' && matchesSearch(session)) {
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
        const sessions = sortSessions(project.sessions.filter((session) => matchesFilter(session) && matchesSearch(session)));
        if (sessions.length === 0) {
            continue;
        }
        visible.push({ cwd: project.cwd, name: project.name, sessions });
    }

    const ordered = logic.sortProjects(visible, state.priorityOrder);

    reconcile(panelsSlot, ordered, (project) => project.cwd, createPanel, updatePanel);
    // A live scan shows its own note (results keep filling in beneath it); then
    // the history-loading note; then, once settled, the empty-filter note when
    // nothing matched.
    if (searchActive && state.searchLoading) {
        stateSlot.innerHTML = emptyBlock(state.labels.search_loading);
    } else if (loadingNote) {
        stateSlot.innerHTML = emptyBlock(loadingNote);
    } else {
        stateSlot.innerHTML = visible.length === 0 ? emptyBlock(state.labels.empty_filter || state.labels.empty_state) : '';
    }

    alignColumns();
    syncOpenMenu();
    syncProcPanel();
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
        state.priorityOrder = localStorage.getItem('amc-priority-order') !== '0';
        state.collapsed = new Set(JSON.parse(localStorage.getItem('amc-collapsed') || '[]'));
        const searchOpts = JSON.parse(localStorage.getItem('amc-search-opts') || '{}');
        state.searchMatchCase = !!searchOpts.matchCase;
        state.searchWholeWord = !!searchOpts.wholeWord;
        state.searchRegex = !!searchOpts.regex;
    } catch (e) {
        state.sort = 'activity';
        state.sortDir = 'asc';
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
