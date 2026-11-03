'use strict';

/* --- sorting (view-level; uses pure helpers from logic.js) --- */

const SORT_VALUES = {
    activity: (session) => (session.age_seconds == null ? Infinity : session.age_seconds),
    usage: (session) => session.usage_total || 0,
    model: (session) => logic.modelRank(session.model),
    // Sorted by the text the host column actually shows, so the order matches
    // what is read there - a background session sorts under "Background", not
    // under the empty-host sentinel its null `host` would put it in. The
    // sentinel still sends a session with no host at all to the end.
    host: (session) => (hostText(session) || '￿').toLowerCase(),
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

function sortLabel(key) {
    const def = SORT_DEFS.find(([sortKey]) => sortKey === key);
    return def ? (state.labels[def[1]] || key) : key;
}

// The thin chevron the dropdown triggers share, drawn rather than typed so it
// stays crisp at any zoom level.
const CARET_SVG = '<svg class="trigger-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="m6 9 6 6 6-6"></path></svg>';

// Two arrows, up and down, marking the dropdown as the sort control.
const SORT_ICON_SVG = '<svg class="trigger-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="m3 8 4-4 4 4M7 4v16M21 16l-4 4-4-4M17 20V4"></path></svg>';

// Sorting is one dropdown, like the filters. Collapsed, its trigger names the
// whole ordering (priority, field, direction); open, it lists the fields, the
// two directions in the chosen field's own words ("newest first" rather than an
// arrow), and the priority ordering as a checkbox. The list stays open while it
// is changed and is updated in place, so the direction wording follows a newly
// picked field on the spot.
function initSortControls() {
    initDropdownTrigger(document.getElementById('sort-trigger'), SORT_ICON_SVG, openSortMenu);
    updateSortControls();
}

// Fill a dropdown trigger (icon, summary slot, chevron) and open its list on
// click. A keyboard activation (no pointer, so detail 0) moves focus into the
// list, which is appended to the end of body and out of the tab order.
function initDropdownTrigger(trigger, iconSvg, openList) {
    trigger.innerHTML = iconSvg + '<span class="trigger-label"></span>' + CARET_SVG;
    trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        openList(trigger, event.detail === 0);
    });
}

function updateSortControls() {
    const label = document.querySelector('#sort-trigger .trigger-label');
    if (label) {
        label.textContent = logic.sortSummary(state.sort, sortLabel(state.sort), state.sortDir, state.priorityOrder, state.labels);
    }
    if (openMenuState && openMenuState.type === 'sort') {
        syncSortMenu(openMenuState.el);
    }
}

function openSortMenu(trigger, focusFirst) {
    if (!claimMenuSlot(trigger)) {
        return;
    }

    const el = document.createElement('div');
    el.className = 'menu sort-menu';
    el.setAttribute('role', 'group');

    const fields = '<div role="radiogroup">'
        + SORT_DEFS.map(([key, labelKey]) => sortItemMarkup('data-sort', key, state.labels[labelKey] || key)).join('')
        + '</div>';
    // The direction labels depend on the field, so syncSortMenu writes them.
    const directions = '<div role="radiogroup">'
        + ['asc', 'desc'].map((dir) => sortItemMarkup('data-dir', dir, '')).join('')
        + '</div>';
    const tip = state.labels.priority_order_hint || '';
    const priority = '<label' + attr('class', 'menu-option') + (tip ? attr('data-tip', tip) : '') + '>'
        + '<input' + attr('type', 'checkbox') + attr('data-priority', '1') + '>'
        + '<span class="menu-label">' + esc(state.labels.priority_order || '') + '</span>'
        + '</label>';
    el.innerHTML = fields + '<div class="menu-separator"></div>' + directions + '<div class="menu-separator"></div>' + priority;

    el.querySelectorAll('button[data-sort]').forEach((button) => {
        button.addEventListener('click', () => setSort(button.dataset.sort));
    });
    el.querySelectorAll('button[data-dir]').forEach((button) => {
        button.addEventListener('click', () => setSortDir(button.dataset.dir));
    });
    const checkbox = el.querySelector('input[data-priority]');
    checkbox.addEventListener('change', () => setPriorityOrder(checkbox.checked));

    syncSortMenu(el);
    showMenu(el, trigger, { type: 'sort' });

    if (focusFirst) {
        const first = el.querySelector('button');
        if (first) {
            first.focus();
        }
    }
}

// One choice in the sort list: a field or a direction, ticked while selected.
// `attrName` is one of two literals, never data.
function sortItemMarkup(attrName, value, label) {
    return '<button' + attr('type', 'button') + attr('class', 'menu-item') + attr('role', 'radio') + attr(attrName, value) + '>'
        + '<span class="menu-check" aria-hidden="true">&#10003;</span>'
        + '<span class="menu-label">' + esc(label) + '</span>'
        + '</button>';
}

function syncSortMenu(el) {
    el.querySelectorAll('button[data-sort]').forEach((button) => {
        const active = button.dataset.sort === state.sort;
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    const directions = logic.sortDirectionOptions(state.sort, state.labels);
    el.querySelectorAll('button[data-dir]').forEach((button) => {
        const option = directions.find((candidate) => candidate.dir === button.dataset.dir);
        const active = button.dataset.dir === state.sortDir;
        button.querySelector('.menu-label').textContent = option ? option.label : '';
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    const checkbox = el.querySelector('input[data-priority]');
    checkbox.checked = state.priorityOrder;
    checkbox.closest('.menu-option').classList.toggle('active', state.priorityOrder);
}

function setSort(key) {
    state.sort = key;
    try {
        localStorage.setItem('amc-sort', state.sort);
    } catch (e) { /* storage unavailable */ }
    afterSortChange();
}

function setSortDir(dir) {
    state.sortDir = dir === 'desc' ? 'desc' : 'asc';
    try {
        localStorage.setItem('amc-sort-dir', state.sortDir);
    } catch (e) { /* storage unavailable */ }
    afterSortChange();
}

// Cross-project ordering. When on (the default), projects are grouped into
// attention bands so the ones that need you sit on top, and within a project
// status leads; when off, projects are a plain alphabetical list and the field
// above alone orders the sessions.
function setPriorityOrder(enabled) {
    state.priorityOrder = !!enabled;
    try {
        localStorage.setItem('amc-priority-order', state.priorityOrder ? '1' : '0');
    } catch (e) { /* storage unavailable */ }
    afterSortChange();
}

function afterSortChange() {
    updateSortControls();
    if (state.last) {
        render(state.last);
    }
}
