'use strict';

/* --- rendering: the session list ---

   The list pane groups sessions by urgency (the sections of logic.LIST_SECTIONS),
   or by project. Every entry is one session; everything beyond what an
   entry shows lives in the detail pane (detail.js) for the selected session.
   Rendering reconciles keyed nodes instead of rebuilding, so the scroll
   position, an open menu and a hovered tooltip survive the per-second poll. */

// Host as plain text, e.g. "VS Code › CLI" for a terminal-driven session
// inside VS Code. "CLI" is a proper name, not translated.
function hostText(session) {
    let text = session.host || '';
    if (session.via_cli) {
        text = text ? text + ' › CLI' : 'CLI';
    }
    if (session.kind && session.kind !== 'interactive') {
        const labelKey = logic.kindLabelKey(session.kind);
        const kindLabel = (labelKey && state.labels[labelKey]) || session.kind;
        text = text ? text + ' · ' + kindLabel : kindLabel;
    }
    return text;
}

// A transcript UTC timestamp as a local date-time with seconds, for the model
// and version histories. Falls back to the raw value if it will not parse.
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

// One registry file per process, so the pid is an entry's stable, unique
// identity. The session id alone is not unique: the same session open in two
// places yields two live records that share it, and duplicate keys would break
// the reconcile pass below.
function rowKey(session) {
    return (session.session_id || 'nosid') + '#' + session.pid;
}

// Whether a click can bring the session's window forward: a past session has no
// process, and a background agent runs under the daemon with no window - the
// title fallback in focus_session_window could otherwise raise an unrelated
// terminal that happens to match.
function hasWindow(session) {
    return !session.is_history && !session.background;
}

// Keyed reconciliation: reuse existing child nodes (matched by data-key),
// create only new ones, remove departed ones, then order to match `items`.
function reconcile(container, items, keyOf, create, update) {
    const existing = new Map();
    for (const child of Array.from(container.children)) {
        if (child.dataset && child.dataset.key != null) {
            existing.set(child.dataset.key, child);
        }
    }

    const ordered = [];
    const seen = new Map();
    for (const item of items) {
        // A duplicate key would leave an orphan node that is never matched nor
        // removed - one per render, without bound. Suffixing it degrades that to
        // a harmless re-created node.
        let key = String(keyOf(item));
        const dupes = seen.get(key) || 0;
        seen.set(key, dupes + 1);
        if (dupes > 0) {
            key += '#dup' + dupes;
        }

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

// Rewrite an element's markup only when it changed: a rewrite drops a hovered
// tooltip's target and a text selection, so the poll must not do it needlessly.
function setMarkup(el, html) {
    if (el.__markup !== html) {
        el.__markup = html;
        el.innerHTML = html;
    }
}

function setText(el, text) {
    const value = text == null ? '' : String(text);
    if (el.textContent !== value) {
        el.textContent = value;
    }
}

function setTip(el, tip) {
    if (tip) {
        el.dataset.tip = tip;
    } else {
        delete el.dataset.tip;
    }
}

/* --- one list entry --- */

// A window with an arrow out of it: open in the full view.
const OPEN_DETAILS_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M15 3h6v6"></path><path d="M10 14 21 3"></path>'
    + '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path></svg>';

function createItem() {
    const el = document.createElement('div');
    el.className = 'item';
    el.innerHTML = '<div class="item-head">'
        +     '<span class="chip"><i></i><span class="chip-label"></span></span>'
        +     '<span class="item-why"></span>'
        + '</div>'
        + '<span class="age"></span>'
        + '<div class="item-title"></div>'
        // What the session waits on, runs or last said - one line.
        + '<div class="item-line"></div>'
        + '<div class="item-meta"></div>'
        // Where a content search shows what it found here; empty (and out of the
        // flow) for every entry it did not match.
        + '<div class="row-hits"></div>'
        // The compact view's way to this session's details in the full view;
        // hidden everywhere else, where a click on the entry already selects it.
        + '<button class="item-open" type="button">' + OPEN_DETAILS_SVG + '</button>';
    return el;
}

function updateItem(el, session) {
    const labels = state.labels;
    const selected = rowKey(session) === state.selectedKey;

    el.className = 'item status-' + session.status
        + (session.is_history ? ' is-history' : '')
        + (hasWindow(session) ? '' : ' no-window')
        + (selected ? ' selected' : '');
    el.setAttribute('aria-selected', selected ? 'true' : 'false');
    el.dataset.session = session.session_id || '';
    el.dataset.project = session.project_name || '';
    el.dataset.title = session.title || '';
    el.dataset.origin = session.origin || 'local';
    if (hasWindow(session)) {
        el.dataset.pid = Number(session.pid);
    } else {
        delete el.dataset.pid;
    }
    if (session.vscode_deeplink) {
        el.dataset.deeplink = '1';
    } else {
        delete el.dataset.deeplink;
    }

    const chip = el.querySelector('.chip');
    setText(chip.querySelector('.chip-label'), session.status_label);
    setTip(chip, session.status_tip);

    const open = el.querySelector('.item-open');
    if (open.dataset.tip !== labels.action_show_details) {
        setTip(open, labels.action_show_details || '');
        open.setAttribute('aria-label', labels.action_show_details || '');
    }

    const why = el.querySelector('.item-why');
    setText(why, session.match_field ? (labels['search_match_' + session.match_field] || '') : '');

    // A live entry's age was captured at the latest snapshot, a past one's at
    // the history fetch; storing that moment per entry lets tickAges advance
    // each from the right base.
    const ageEl = el.querySelector('.age');
    if (session.age_seconds != null) {
        const ageAt = session.is_history ? state.historyReceivedAt : state.receivedAt;
        ageEl.dataset.age = session.age_seconds;
        ageEl.dataset.ageAt = ageAt != null ? ageAt : Date.now();
        setText(ageEl, logic.formatAgeSince(session.age_seconds, Number(ageEl.dataset.ageAt), Date.now(), labels));
    } else {
        delete ageEl.dataset.age;
        delete ageEl.dataset.ageAt;
        setText(ageEl, '');
    }

    const title = el.querySelector('.item-title');
    setText(title, session.name);
    setTip(title, session.name);

    const line = el.querySelector('.item-line');
    setMarkup(line, activityMarkup(session.activity, session.is_history ? state.historyReceivedAt : state.receivedAt));
    fillTicks(line);
    setMarkup(el.querySelector('.item-meta'), itemMetaMarkup(session));

    // The passages the content search found, only the first one here - the
    // detail shows them all. A match is never mutated in place, so identity is
    // an exact test for "unchanged" and a selection inside it survives the poll.
    const hitsEl = el.querySelector('.row-hits');
    const hit = searchHitFor(session.session_id);
    if (hitsEl.__searchHit !== hit) {
        hitsEl.__searchHit = hit;
        hitsEl.innerHTML = logic.searchHitsMarkup(hit, labels, 1);
    }
}

// Shorten each graph label to the width its box leaves, measured in the font it
// is drawn in - a character count cannot know how wide a bold "W" is. Done once
// per freshly written label; the full text stays in the node's tooltip.
// The fitted form of each label already measured, by its class and text: a
// rebuilt graph repeats nearly all of them, and every measurement forces a
// layout - a thousand labels measured afresh on each poll froze the window.
const graphTextFits = new Map();
const GRAPH_TEXT_FITS_MAX = 5000;

function fitGraphText(root) {
    root.querySelectorAll('.gnode text').forEach((text) => {
        if (text.__fitted) {
            return;
        }
        text.__fitted = true;
        const cacheKey = text.getAttribute('class') + '|' + text.textContent;
        if (graphTextFits.has(cacheKey)) {
            text.textContent = graphTextFits.get(cacheKey);
            return;
        }
        fitOneGraphText(text);
        if (graphTextFits.size >= GRAPH_TEXT_FITS_MAX) {
            graphTextFits.clear();
        }
        graphTextFits.set(cacheKey, text.textContent);
    });
}

// Shorten one label to the room its box has, by halving the length that fits.
// Measured on a canvas in the label's own font, which needs no layout: an SVG
// text's own measurement forces one on every call.
function fitOneGraphText(text) {
    const measure = graphTextMeasurer(text);
    const full = text.textContent.replace(/…$/, '');
    if (measure(full) <= logic.GRAPH_TEXT_WIDTH) {
        text.textContent = full;
        return;
    }
    let fits = 0;
    let tooLong = full.length;
    while (tooLong - fits > 1) {
        const middle = Math.floor((fits + tooLong) / 2);
        if (measure(full.slice(0, middle).trimEnd() + '…') <= logic.GRAPH_TEXT_WIDTH) {
            fits = middle;
        } else {
            tooLong = middle;
        }
    }
    text.textContent = full.slice(0, fits).trimEnd() + '…';
}

// A width function for the font of `text`'s class, read once per class.
const graphTextFonts = new Map();
let graphTextCanvas = null;

function graphTextMeasurer(text) {
    const className = text.getAttribute('class') || '';
    if (!graphTextFonts.has(className)) {
        const style = getComputedStyle(text);
        graphTextFonts.set(className, style.fontWeight + ' ' + style.fontSize + ' ' + style.fontFamily);
    }
    if (!graphTextCanvas) {
        graphTextCanvas = document.createElement('canvas').getContext('2d');
    }
    const context = graphTextCanvas;
    const font = graphTextFonts.get(className);
    return (value) => {
        context.font = font;
        return context.measureText(value).width;
    };
}

// The activity line: the call or question the session waits on, the call it
// runs with a ticking duration, or the first line of its last reply.
function activityMarkup(activity, capturedAt) {
    if (!activity || !activity.text) {
        return '';
    }
    let html = '<span' + attr('class', 'act act-' + activity.kind) + '>' + esc(activity.text) + '</span>';
    if (activity.since != null) {
        html += tickMarkup(activity.since, capturedAt);
    }
    return html;
}

// A duration that tickAges advances. The markup names only the moment it
// began, to the second, so the next poll writes the same markup and a text
// selection beside it survives; fillTicks writes the text once it is in place.
function tickMarkup(seconds, capturedAt) {
    const elapsed = Number(seconds);
    if (seconds == null || !Number.isFinite(elapsed)) {
        return '';
    }
    const at = capturedAt != null ? capturedAt : Date.now();
    const began = Math.round((at - elapsed * 1000) / 1000) * 1000;
    return '<span class="tick"' + attr('data-age', 0) + attr('data-age-at', began) + '></span>';
}

// Write the current text of every ticking age and duration inside *root*.
function fillTicks(root) {
    if (!root) {
        return;
    }
    const now = Date.now();
    root.querySelectorAll('.age[data-age], .tick[data-age]').forEach((el) => {
        el.textContent = logic.formatAgeSince(Number(el.dataset.age), Number(el.dataset.ageAt), now, state.labels);
    });
}

// The line under the title: the project (unless the group already names it),
// the cost, and the first signals in their rank order.
function itemMetaMarkup(session) {
    const parts = [];
    if (state.group !== 'project' && session.project_name) {
        parts.push('<span class="meta-project">' + esc(session.project_name) + '</span>');
    }
    if (session.usage_compact) {
        parts.push('<span class="meta-cost">' + esc(session.usage_compact) + '</span>');
    }
    const signals = logic.sessionSignals(session, state.labels).slice(0, logic.LIST_SIGNAL_LIMIT);
    for (const signal of signals) {
        parts.push('<span' + attr('class', 'sig sig-' + signal.tone) + '>' + esc(signal.text) + '</span>');
    }
    return parts.join('');
}

// This session's search result, or null when no search matched it.
function searchHitFor(sessionId) {
    if (!(state.searchMatches instanceof Map)) {
        return null;
    }
    return state.searchMatches.get(sessionId) || null;
}

/* --- sections (urgency grouping) --- */

function createSection() {
    const el = document.createElement('section');
    el.className = 'list-section';
    el.innerHTML = '<button class="section-head" type="button">'
        +     '<span class="chevron" aria-hidden="true">&#9662;</span>'
        +     '<span class="section-dot"></span>'
        +     '<span class="section-label"></span>'
        +     '<span class="section-count"></span>'
        +     '<span class="section-note"></span>'
        + '</button>'
        + '<div class="section-ranges"></div>'
        + '<div class="section-note-line"></div>'
        + '<div class="section-items"></div>';
    return el;
}

function updateSection(el, section) {
    const labels = state.labels;
    el.className = 'list-section section-' + section.key + (section.open ? '' : ' closed');

    const head = el.querySelector('.section-head');
    head.dataset.section = section.key;
    head.setAttribute('aria-expanded', section.open ? 'true' : 'false');
    setText(head.querySelector('.section-label'), labels[section.label] || section.key);
    setText(head.querySelector('.section-count'), section.count == null ? '' : section.count);
    setText(head.querySelector('.section-note'), section.note || '');

    setMarkup(el.querySelector('.section-ranges'), section.key === 'older' && section.open ? historyRangeMarkup() : '');
    setText(el.querySelector('.section-note-line'), section.noteLine || '');

    reconcile(el.querySelector('.section-items'), section.items, rowKey, createItem, updateItem);
}

// How far back "Older" reaches, as a row of buttons under its header.
function historyRangeMarkup() {
    return logic.HISTORY_RANGES.map((range) => '<button'
        + attr('type', 'button')
        + attr('class', 'range-btn' + (range.key === state.historyRange ? ' active' : ''))
        + attr('data-range', range.key)
        + attr('aria-pressed', range.key === state.historyRange ? 'true' : 'false')
        + '>' + esc(state.labels[range.label] || range.key) + '</button>').join('');
}

// How far back the graph reaches, as a row of buttons above it.
function graphRangeMarkup() {
    return logic.GRAPH_RANGES.map((range) => '<button'
        + attr('type', 'button')
        + attr('class', 'range-btn' + (range.key === state.graphRange ? ' active' : ''))
        + attr('data-graph-range', range.key)
        + attr('aria-pressed', range.key === state.graphRange ? 'true' : 'false')
        + '>' + esc(state.labels[range.label] || range.key) + '</button>').join('')
        + '<span class="chips-gap"></span>'
        + '<button' + attr('type', 'button') + attr('class', 'range-btn') + attr('data-graph-collapse', 'all') + '>'
        + esc(state.labels.graph_collapse_all || '') + '</button>'
        + '<button' + attr('type', 'button') + attr('class', 'range-btn') + attr('data-graph-collapse', 'none') + '>'
        + esc(state.labels.graph_expand_all || '') + '</button>';
}

/* --- groups (project grouping) --- */

function createGroup() {
    const el = document.createElement('section');
    el.className = 'list-group';
    el.innerHTML = '<div class="group-head">'
        +     '<span class="chevron" aria-hidden="true">&#9662;</span>'
        +     '<span class="group-name"></span>'
        +     '<span class="group-origin"></span>'
        +     '<span class="group-stats"></span>'
        + '</div>'
        + '<div class="group-path"></div>'
        + '<div class="group-items"></div>';
    return el;
}

function updateGroup(el, group) {
    const collapsed = state.collapsed.has(group.key);
    el.className = 'list-group' + (collapsed ? ' closed' : '');

    const head = el.querySelector('.group-head');
    head.dataset.groupKey = group.key;
    setText(head.querySelector('.group-name'), group.name);
    setText(head.querySelector('.group-origin'), group.origin_display || '');
    setMarkup(head.querySelector('.group-stats'), groupStatsMarkup(group));

    // The project path is its own click target: it opens the folder.
    const pathEl = el.querySelector('.group-path');
    setMarkup(pathEl, group.cwd
        ? '<span class="path-open"' + attr('data-cwd', group.cwd) + attr('data-origin', group.origin || 'local')
            + attr('data-tip', state.labels.show_in_file_manager || '') + '>' + esc(group.cwd) + '</span>'
        : '');

    const items = el.querySelector('.group-items');
    if (collapsed) {
        items.replaceChildren();
    } else {
        reconcile(items, group.sessions, rowKey, createItem, updateItem);
    }
}

// The group header's figures: how many sessions need you, how many there are,
// and what they cost.
function groupStatsMarkup(group) {
    const labels = state.labels;
    const needs = group.sessions.filter((session) => session.status === 'awaiting_permission').length;
    const parts = [];
    if (needs > 0) {
        parts.push('<span class="group-needs"' + attr('data-tip', labels.section_needs || '') + '>&#9679; ' + esc(needs) + '</span>');
    }
    parts.push('<span>' + esc(group.sessions.length) + '</span>');
    const cost = groupCost(group);
    if (cost) {
        parts.push('<span>' + esc(cost) + '</span>');
    }
    return parts.join('');
}

function groupCost(group) {
    let sum = 0;
    for (const session of group.sessions) {
        if (session.cost_usd == null) {
            return '';
        }
        sum += session.cost_usd;
    }
    return logic.formatCost(sum) || '';
}

/* --- the section chips (project grouping) --- */

// With the list grouped by project there are no section headers to
// open and close, so the sections become a row of toggles above the groups.
function sectionChipsMarkup(sections) {
    const labels = state.labels;
    return sections.map((section) => '<button'
        + attr('type', 'button')
        + attr('class', 'section-chip section-' + section.key)
        + attr('data-section', section.key)
        + attr('aria-pressed', section.active ? 'true' : 'false')
        + '><i></i>' + esc(labels[section.label] || section.key)
        + (section.count == null ? '' : ' <b>' + esc(section.count) + '</b>')
        + '</button>').join('');
}

/* --- collapse state of the project groups --- */

function persistCollapsed() {
    try {
        localStorage.setItem('amc-collapsed', JSON.stringify([...state.collapsed]));
    } catch (e) { /* storage unavailable */ }
}

function toggleCollapse(key) {
    if (!key) {
        return;
    }
    if (state.collapsed.has(key)) {
        state.collapsed.delete(key);
    } else {
        state.collapsed.add(key);
    }
    persistCollapsed();
    if (state.last) {
        render(state.last);
    }
}

// The one place UI state is mapped onto the plain data the pure markup builders
// in logic.js take, so their wording rules stay testable without a DOM.
function emptyStateView(searchActive, quietMessage, loadingNote) {
    return {
        allKeys: FILTER_KEYS,
        activeKeys: state.filters,
        includeHidden: state.searchIncludeHidden,
        historyRange: state.historyRange,
        searchActive,
        // A pending debounce counts as running: until that scan starts, an
        // empty list says nothing about the query being typed.
        searching: Boolean(state.searchLoading || state.searchTimer),
        quietMessage,
        loadingNote,
        labels: state.labels,
    };
}

/* --- the shell --- */

let chipsSlot = null;
let noticeSlot = null;
let listSlot = null;
let stateSlot = null;
let graphSlot = null;

function ensureShell() {
    if (listSlot && listSlot.isConnected) {
        return;
    }
    const content = document.getElementById('content');
    content.innerHTML = '<div class="section-chips"></div><div class="notice-slot"></div>'
        + '<div class="list-slot"></div><div class="graph-slot"></div><div class="state-slot"></div>';
    chipsSlot = content.querySelector('.section-chips');
    noticeSlot = content.querySelector('.notice-slot');
    listSlot = content.querySelector('.list-slot');
    stateSlot = content.querySelector('.state-slot');
    graphSlot = content.querySelector('.graph-slot');
}

// The sessions as the list shows them, in display order, with the query's
// meta match on each. Returns what render and the keyboard need.
function render(snapshot) {
    const previous = state.last;
    state.last = snapshot;

    const labels = state.labels;
    const prices = logic.resolvePrices(state.pricing, todayIso());
    const query = state.search.trim();
    const searchActive = query.length >= SEARCH_MIN_CHARS;
    const options = searchOptions();

    // A widened search reads past the closed sections, so its extra hits pass
    // the section filter too. Only a settled hit counts.
    const widened = searchActive && state.searchIncludeHidden;
    const isWidenedHit = (session) => widened
        && state.searchMatches instanceof Map
        && state.searchMatches.has(session.session_id);

    // The past sessions the selected window covers, minus any that came back to
    // life. Folded into the list while "Older" is open, or while a widened
    // search reaches into them.
    const historyActive = state.filters.has('history');
    const historyRows = Array.isArray(state.history)
        ? logic.pruneResumedHistory(visibleHistory(), snapshot.sessions || [])
        : [];
    const foldHistory = (historyActive || widened) && Array.isArray(state.history);
    let rawSessions = snapshot.sessions || [];
    if (foldHistory) {
        rawSessions = rawSessions.concat(historyRows);
        // A live session that just left the snapshot was excluded from the
        // one-shot history fetch; re-fetch so it moves into "Older" instead of
        // vanishing from both.
        if (previous && logic.historyNeedsRefresh(previous.sessions, snapshot.sessions)) {
            state.history = null;
            state.historyLoadedSeconds = undefined;
            ensureHistoryLoaded();
        }
    }
    const loadingNote = ((historyActive || widened) && state.historyLoading) ? labels.history_loading : '';

    const projects = logic.groupProjects(rawSessions, labels, prices);
    const sessions = logic.flattenProjects(projects);

    // The query filters on the session's own fields at once and on its content
    // as the scan reports matches.
    const matchesQuery = (session) => {
        session.match_field = searchActive ? logic.sessionMetaMatch(session, query, options) : null;
        return !searchActive || Boolean(session.match_field) || logic.sessionMatchesSearch(session.session_id, true, state.searchMatches);
    };
    const found = [];
    for (const session of sessions) {
        if (matchesQuery(session)) {
            found.push(session);
        }
    }

    reportAttention(sessions);
    updateToolbarSummary(snapshot, prices);
    ensureShell();

    // Section counts follow the query, like the list. "Older" counts its
    // sessions from the cache while it is closed, since they are not folded in -
    // built like the list's, so a title match counts there as it shows there.
    const sectionData = logic.buildSections(found, state.filters);
    const counts = new Map(sectionData.map((section) => [section.key, section.sessions.length]));
    if (!foldHistory) {
        let olderCount = null;
        if (Array.isArray(state.history)) {
            olderCount = searchActive
                ? logic.flattenProjects(logic.groupProjects(historyRows, labels, prices)).filter(matchesQuery).length
                : historyRows.length;
        }
        counts.set('older', olderCount);
    }

    const compact = state.compact;
    const grouping = compact ? 'urgency' : state.group;
    document.body.classList.toggle('group-urgency', grouping === 'urgency');
    document.body.classList.toggle('group-graph', grouping === 'graph');
    applyListWidth();

    let outsideFilters = 0;
    const shown = [];
    if (grouping !== 'graph') {
        setMarkup(graphSlot, '');
    }
    if (grouping === 'graph') {
        // The live sessions the query leaves, with their subagents and the
        // messages between them; the sections do not apply here.
        // The chip row holds the graph's time window here. Through setMarkup,
        // so its cache sees the slot change and the toggles come back when the
        // Project grouping writes the same markup again.
        setMarkup(chipsSlot, graphRangeMarkup());
        listSlot.replaceChildren();
        const graphSessions = found.filter((session) => !session.is_history);
        shown.push(...graphSessions);
        const entries = graphSessions.map((session) => ({ key: rowKey(session), session: session }));
        const selection = { key: state.selectedKey, agent: state.selectedAgent, process: state.selectedProcess, tool: state.selectedTool };
        const model = logic.graphModel(entries, logic.graphRange(state.graphRange).seconds, selection, state.graphCollapsed);
        setMarkup(graphSlot, graphSessions.length > 0 ? logic.graphMarkup(logic.graphLayout(model), labels, selection) : '');
        fitGraphText(graphSlot);
    } else if (grouping === 'urgency') {
        setMarkup(chipsSlot, '');
        const sections = sectionData.map((section) => {
            const open = section.active;
            const items = open ? section.sessions : section.sessions.filter(isWidenedHit);
            if (!section.active) {
                outsideFilters += items.length;
            }
            shown.push(...items);
            return {
                key: section.key,
                label: section.label,
                open: open,
                items: items,
                count: counts.get(section.key),
                note: sectionNote(section.key, open),
                noteLine: section.key === 'older' && open ? loadingNote : '',
            };
        });
        if (!(listSlot.firstElementChild && listSlot.firstElementChild.classList.contains('list-sections'))) {
            listSlot.innerHTML = '<div class="list-sections"></div>';
        }
        reconcile(listSlot.firstElementChild, sections, (section) => section.key, createSection, updateSection);
    } else {
        setMarkup(chipsSlot, sectionChipsMarkup(sectionData.map((section) => ({
            key: section.key, label: section.label, active: section.active, count: counts.get(section.key),
        }))));

        const visible = [];
        for (const session of found) {
            if (matchesFilter(session)) {
                visible.push(session);
            } else if (isWidenedHit(session)) {
                visible.push(session);
                outsideFilters += 1;
            }
        }
        const groups = projectGroups(projects, visible);
        for (const group of groups) {
            if (!state.collapsed.has(group.key)) {
                shown.push(...group.sessions);
            }
        }
        if (!(listSlot.firstElementChild && listSlot.firstElementChild.classList.contains('list-groups'))) {
            listSlot.innerHTML = '<div class="list-groups"></div>';
        }
        reconcile(listSlot.firstElementChild, groups, (group) => group.key, createGroup, updateGroup);
    }

    state.listKeys = shown.map(rowKey);
    state.sessionsByKey = new Map(sessions.map((session) => [rowKey(session), session]));

    // Rows on screen against their section's filter: say so, or they read as a
    // filter that quietly stopped working.
    setMarkup(noticeSlot, outsideFilters > 0 ? logic.widenedNoticeMarkup(outsideFilters, labels) : '');

    // A running scan, a history load, or nothing left in view. With sessions in
    // the sections, an open "Older" says it is loading in its own header.
    // With the sections on screen, closing all of them empties the list on
    // purpose: their headers and counts already say what there is, so only no
    // agents at all, a search or a load gets a message.
    const noteHere = grouping !== 'project' && shown.length > 0 ? '' : loadingNote;
    const sectionsSpeak = grouping === 'urgency' && !searchActive && !noteHere && sessions.length > 0;
    if ((shown.length > 0 && !(searchActive && state.searchLoading) && !noteHere) || sectionsSpeak) {
        setMarkup(stateSlot, '');
    } else {
        const quiet = sessions.length === 0 ? labels.empty_state : (labels.empty_filter || labels.empty_state);
        setMarkup(stateSlot, logic.emptyStateMarkup(emptyStateView(searchActive, quiet, noteHere)));
    }

    ensureSelection();
    renderDetail();
    syncOpenMenu();
    syncTooltip();
}

// The project groups of the visible sessions, in the attention order of
// logic.sortProjects, each one's sessions most urgent first.
function projectGroups(projects, visible) {
    const byProject = new Map();
    for (const session of visible) {
        if (!byProject.has(session.project_key)) {
            byProject.set(session.project_key, []);
        }
        byProject.get(session.project_key).push(session);
    }
    const groups = [];
    for (const project of projects) {
        const sessions = byProject.get(project.key);
        if (sessions && sessions.length > 0) {
            groups.push({ ...project, sessions: logic.sortGroupSessions(sessions) });
        }
    }
    return logic.sortProjects(groups, true);
}

// The note at the right of a section header: the window "Older" reaches, so a
// closed section still says what opening it would list.
function sectionNote(sectionKey, open) {
    if (sectionKey !== 'older' || open) {
        return '';
    }
    return state.labels[logic.historyRange(state.historyRange).label] || '';
}

// Keep a selection while its session is listed, and otherwise move it to the
// first entry, so the detail never describes a session the list hides.
function ensureSelection() {
    if (state.selectedKey && state.listKeys.includes(state.selectedKey)) {
        return;
    }
    state.selectedKey = state.listKeys.length > 0 ? state.listKeys[0] : null;
    state.selectedAgent = null;
    state.selectedProcess = null;
    state.detailOpen = false;
    document.body.classList.remove('detail-open');
    markSelectedItem();
}

// Move the selection mark without a full render.
function markSelectedItem() {
    document.querySelectorAll('.item.selected').forEach((el) => {
        el.classList.remove('selected');
        el.setAttribute('aria-selected', 'false');
    });
    // A node is marked when everything it names matches the selection: its
    // session, and its subagent, process or tool, or none of them.
    document.querySelectorAll('.gnode[data-key]').forEach((el) => {
        const agent = el.dataset.agent || null;
        const process = el.dataset.process ? Number(el.dataset.process) : null;
        const tool = el.dataset.tool || null;
        el.classList.toggle('selected', el.dataset.key === state.selectedKey && agent === (state.selectedAgent || null)
            && process === (state.selectedProcess || null) && tool === (state.selectedTool || null));
    });
    if (!state.selectedKey) {
        return;
    }
    const el = findItem(state.selectedKey);
    if (el) {
        el.classList.add('selected');
        el.setAttribute('aria-selected', 'true');
    }
}

function findItem(key) {
    if (!key) {
        return null;
    }
    for (const el of document.querySelectorAll('.item')) {
        if (el.dataset.key === key) {
            return el;
        }
    }
    return null;
}

// The count of active sessions and their cost, at the right of the toolbar.
function updateToolbarSummary(snapshot, prices) {
    const el = document.getElementById('toolbar-summary');
    if (!el) {
        return;
    }
    const live = snapshot.sessions || [];
    let total = 0;
    let priced = live.length > 0;
    for (const raw of live) {
        const usd = raw.scan_pending ? null : logic.sessionTotalCostUsd(raw, prices);
        if (usd === null) {
            priced = false;
            break;
        }
        total += usd;
    }
    const count = '<b>' + esc(live.length) + '</b>';
    const label = esc(state.labels.summary_active || '').replace('{count}', count);
    setMarkup(el, label + (priced ? ' · <b>' + esc(logic.formatCost(total)) + '</b>' : ''));
}

// Tell the window how many sessions are blocked on you, and whether one of them
// just started to - the taskbar counts the first and flashes for the second.
function reportAttention(sessions) {
    const update = logic.attentionUpdate(state.attentionKeys, sessions);
    const previousCount = state.attentionKeys instanceof Set ? state.attentionKeys.size : null;
    state.attentionKeys = update.keys;
    if (previousCount === update.count && !update.newlyWaiting) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.set_attention === 'function') {
        logic.settleCall(() => bridge.set_attention(update.count, update.newlyWaiting));
    }
}

function renderLoading() {
    document.getElementById('content').innerHTML = '<div class="loading">'
        + '<div class="skeleton-row"></div><div class="skeleton-row"></div><div class="skeleton-row"></div>'
        + '<div class="skeleton-row"></div></div>';
}
