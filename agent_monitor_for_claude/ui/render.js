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

/* --- where each listed session's work stands in git ---

   Asked of the bridge in a loop of its own, never on the render path: the
   bridge answers from its cache at once and reads git in the background, so
   the list shows first and the git signals follow. */
const GIT_OVERVIEW_MS = 4000;
const DESKTOP_GROUPS_MS = 5000;
const PIN_CHECK_MS = 60000;
const GIT_OVERVIEW_PENDING_MS = 1000;

// Give each session its directory's latest git record, with the age of the
// remote's picture as of now.
function attachGitState(sessions) {
    const nowSeconds = Date.now() / 1000;
    for (const session of sessions) {
        const record = state.gitOverview.get(logic.gitDirKey(session));
        session.git = record && typeof record === 'object'
            ? Object.assign({}, record, { fetched_seconds: Number.isFinite(record.fetched_at) ? nowSeconds - record.fetched_at : null })
            : null;
    }
}

// Ask for every listed directory once, then again: soon while a first answer
// is still pending, else every GIT_OVERVIEW_MS. A changed answer re-renders.
async function gitOverviewLoop() {
    let pending = false;
    try {
        pending = await refreshGitOverview();
    } catch (err) {
        // A bridge hiccup - the next round asks again.
    }
    setTimeout(gitOverviewLoop, pending ? GIT_OVERVIEW_PENDING_MS : GIT_OVERVIEW_MS);
}

// Follow Claude Desktop's groups; a changed answer re-renders.
async function desktopGroupsLoop() {
    try {
        await refreshDesktopGroups();
    } catch (err) {
        // A bridge hiccup - the next round asks again.
    }
    setTimeout(desktopGroupsLoop, DESKTOP_GROUPS_MS);
}

// Ask whether the pinned sessions still exist; one that is missing twice in a
// row is dropped, an unreadable answer keeps it.
async function pinCheckLoop() {
    try {
        await refreshPins();
    } catch (err) {
        // A bridge hiccup - the next round asks again.
    }
    setTimeout(pinCheckLoop, PIN_CHECK_MS);
}

async function refreshPins() {
    const bridge = apiBridge();
    if (state.pins.size === 0 || !bridge || typeof bridge.check_pins !== 'function') {
        return;
    }
    const refs = Array.from(state.pins, ([id, origin]) => ({ id: id, origin: origin }));
    const asked = new Set(state.pins.keys());
    const status = await bridge.check_pins(refs);
    const result = logic.prunePins(state.pins, status, state.pinMisses);
    state.pinMisses = result.misses;
    // A pin added while the bridge answered was not asked about: keep it.
    for (const [id, origin] of state.pins) {
        if (!asked.has(id)) {
            result.pins.set(id, origin);
        }
    }
    if (result.pins.size === state.pins.size) {
        return;
    }
    state.pins = result.pins;
    savePins();
    if (state.last) {
        render(state.last);
    }
    tick();
}

async function refreshDesktopGroups() {
    const bridge = apiBridge();
    const raw = bridge && typeof bridge.get_desktop_groups === 'function' ? await bridge.get_desktop_groups() : window.__MOCK_DESKTOP_GROUPS__;
    const next = logic.normalizeDesktopGroups(raw);
    // A Desktop pin replaces an own pin: only an id this answer names drops one.
    const pins = logic.pinsWithoutStarred(state.pins, next.starred);
    const pinsChanged = pins !== state.pins;
    if (pinsChanged) {
        state.pins = pins;
        savePins();
    }
    if (!pinsChanged && logic.desktopGroupsSignature(next) === logic.desktopGroupsSignature(state.desktopGroups)) {
        return;
    }
    state.desktopGroups = next;
    if (state.last) {
        render(state.last);
    }
}

async function refreshGitOverview() {
    // The listed entries first: the backend answers at most a few hundred
    // directories, and those are the ones on screen.
    const refs = new Map();
    const listed = (state.listKeys || []).map((key) => state.sessionsByKey.get(key)).filter(Boolean);
    for (const session of [...listed, ...state.sessionsByKey.values()]) {
        const key = logic.gitDirKey(session);
        if (key && !refs.has(key)) {
            refs.set(key, { session_id: session.session_id || '', cwd: session.cwd || '', worktree_cwd: session.worktree_cwd || '',
                origin: session.origin || 'local' });
        }
    }
    if (refs.size === 0) {
        return false;
    }
    const keys = Array.from(refs.keys());
    const answers = await callGitOverview(Array.from(refs.values()));
    let pending = false;
    let changed = false;
    keys.forEach((key, index) => {
        const answer = answers[index];
        if (answer === 'pending') {
            pending = true;
            return;
        }
        const value = answer && typeof answer === 'object' ? answer : null;
        if (JSON.stringify(state.gitOverview.get(key) || null) !== JSON.stringify(value)) {
            state.gitOverview.set(key, value);
            changed = true;
        }
    });
    if (changed && state.last) {
        render(state.last);
    }
    return pending;
}

async function callGitOverview(refs) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_git_overview === 'function') {
        const answers = await bridge.get_git_overview(refs);
        return Array.isArray(answers) ? answers : [];
    }
    const mock = window.__MOCK_GIT_OVERVIEW__ || {};
    return refs.map((ref) => mock[ref.worktree_cwd || ref.cwd] || null);
}

// The processes of the session under `key` that ended lately (see trackEndedProcesses).
function endedProcessesOf(key) {
    const tracker = state.processTracker;
    return (tracker && tracker.ended.get(key)) || [];
}

// Whether a click can bring the session's window forward: a past session has no
// process - nor has a pinned or grouped one kept listed after it ended - and a
// background agent runs under the daemon with no window - the title fallback in
// focus_session_window could otherwise raise an unrelated terminal that happens
// to match.
function hasWindow(session) {
    return !session.is_history && !session.background && !isEndedKept(session);
}

// A pinned or grouped session listed after it ended, from its transcript.
function isEndedKept(session) {
    return session.kept === true && session.alive !== true;
}

// Whether the session may be offered for deletion: only a past one - a History
// row, or a kept one that ended. The backend checks again that no process holds it.
function canDeleteSession(session) {
    return Boolean(session.is_history) || isEndedKept(session);
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
        // Claude Desktop archived this session while it still runs.
        +     '<span class="item-flag"></span>'
        // Under Pinned, where a Claude Desktop chat's pin comes from.
        +     '<span class="item-pin-source"></span>'
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
        + '<button class="item-open" type="button">' + OPEN_DETAILS_SVG + '</button>'
        // How much of its prompt cache's lifetime the session has left, as a
        // bar along the entry's foot that shrinks as it runs out.
        + '<div class="item-cache" aria-hidden="true"><i></i></div>';
    return el;
}

function updateItem(el, session, pinned) {
    const labels = state.labels;
    const selected = rowKey(session) === state.selectedKey;

    el.className = 'item status-' + session.status
        + (session.is_history ? ' is-history' : '')
        + (hasWindow(session) ? '' : ' no-window')
        + (session.cache ? ' has-cache' : '')
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

    // The compact view has one line per session and leaves the mark out.
    setText(el.querySelector('.item-flag'), session.archived && !state.compact ? labels.label_archived || '' : '');
    const pinSource = pinned && !state.compact
        ? logic.pinSource(session, state.pins, state.desktopGroups.starred, state.desktopGroups.members) : null;
    setMarkup(el.querySelector('.item-pin-source'), logic.pinSourceMarkup(pinSource, labels));

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
    setMarkup(line, itemActivityMarkup(session.activity, session.is_history ? state.historyReceivedAt : state.receivedAt));
    fillTicks(line);
    const meta = el.querySelector('.item-meta');
    setMarkup(meta, itemMetaMarkup(session));
    updateCacheBar(el.querySelector('.item-cache'), meta.querySelector('.meta-cache'));
    fillCacheTicks(el);

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
        } else {
            fitOneGraphText(text);
            if (graphTextFits.size >= GRAPH_TEXT_FITS_MAX) {
                graphTextFits.clear();
            }
            graphTextFits.set(cacheKey, text.textContent);
        }
        dropRedundantGraphTip(text);
    });
}

// A node's tooltip only completes a title its box cut short; one that fits
// whole would repeat it.
function dropRedundantGraphTip(text) {
    if (!text.classList.contains('gnode-label') || text.textContent.endsWith('…')) {
        return;
    }
    const node = text.closest('.gnode');
    if (node) {
        node.removeAttribute('data-tip');
    }
}

// Shorten one label to the room its box has, by halving the length that fits.
// Measured on a canvas in the label's own font, which needs no layout: an SVG
// text's own measurement forces one on every call.
function fitOneGraphText(text) {
    const measure = graphTextMeasurer(text);
    // A label that fits stays as written, an ellipsis of its own included; one
    // that does not loses the ellipsis clipLabel may have added before shrinking.
    if (measure(text.textContent) <= logic.GRAPH_TEXT_WIDTH) {
        return;
    }
    const full = text.textContent.replace(/…$/, '');
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
function itemActivityMarkup(activity, capturedAt) {
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
    const began = recentBegin(Math.round((at - elapsed * 1000) / 1000) * 1000);
    return '<span class="tick"' + attr('data-age', 0) + attr('data-age-at', began) + '></span>';
}

// The start already written for a moment within a second of *began*, else
// *began*: the age arrives with a latency that differs from poll to poll, and
// rounded near a half second the start flipped between two values - rewriting
// the markup, and dropping a selection beside it, with nothing changed.
const RECENT_BEGINS_KEPT = 64;
const recentBegins = [];

function recentBegin(began) {
    const known = recentBegins.find((value) => Math.abs(value - began) <= 1000);
    if (known !== undefined) {
        return known;
    }
    recentBegins.push(began);
    if (recentBegins.length > RECENT_BEGINS_KEPT) {
        recentBegins.shift();
    }
    return began;
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
    fillCacheTicks(root);
}

// The cache bar of an entry follows the moment its cache figure names; an entry
// without one has no bar.
function updateCacheBar(bar, figure) {
    if (!bar) {
        return;
    }
    if (figure) {
        bar.dataset.cacheUntil = figure.dataset.cacheUntil;
        bar.dataset.cacheTtl = figure.dataset.cacheTtl;
    } else {
        delete bar.dataset.cacheUntil;
        delete bar.dataset.cacheTtl;
        delete bar.dataset.moving;
        bar.className = 'item-cache';
        bar.firstElementChild.style.width = '';
    }
}

// Write what every cache figure and bar inside *root* says now: the time left,
// the phase it is in and the share of its lifetime the bar still covers.
function fillCacheTicks(root) {
    if (!root) {
        return;
    }
    const now = Date.now();
    root.querySelectorAll('[data-cache-until]').forEach((el) => {
        const remaining = (Number(el.dataset.cacheUntil) - now) / 1000;
        const ttl = Number(el.dataset.cacheTtl);
        const phase = logic.cachePhase(remaining, ttl);
        if (el.classList.contains('item-cache')) {
            setClass(el, 'item-cache cache-' + phase);
            const fill = el.firstElementChild;
            // The first width is set as it stands; only the seconds after it glide,
            // or a new entry would show an empty track filling up.
            if (fill.style.width) {
                el.dataset.moving = '';
            }
            fill.style.width = (logic.cacheShareLeft(remaining, ttl) * 100).toFixed(2) + '%';
            return;
        }
        setClass(el, 'meta-cache cache-' + phase);
        setText(el, logic.cacheListText(Math.max(0, Math.floor(remaining)), el.dataset.cacheCost || '', state.labels));
    });
}

function setClass(el, className) {
    if (el.className !== className) {
        el.className = className;
    }
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
    if (state.listContext && session.context && session.context.ratio !== null) {
        parts.push('<span' + attr('class', 'meta-context' + (session.context_warn ? ' warn' : '')) + '>'
            + esc(fmt(state.labels.list_context, { percent: Math.round(session.context.ratio * 100) })) + '</span>');
    }
    parts.push(logic.cacheMetaMarkup(session.cache, state.receivedAt, state.labels));
    const signals = logic.sessionSignals(session, state.labels).slice(0, logic.LIST_SIGNAL_LIMIT);
    for (const signal of signals) {
        parts.push('<span' + attr('class', 'sig sig-' + signal.tone) + (signal.tip ? attr('data-tip', signal.tip) : '') + '>'
            + esc(signal.text) + '</span>');
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

// The sections that list past sessions and share the History window.
const PAST_SECTIONS = new Set(['older', 'archived']);

function createSection(section) {
    if (section.kind === 'group') {
        return createGroupSection();
    }
    if (section.kind === 'divider') {
        const divider = document.createElement('div');
        divider.className = 'list-divider';
        divider.setAttribute('role', 'heading');
        divider.setAttribute('aria-level', '2');
        return divider;
    }
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
    if (section.kind === 'group') {
        updateGroupSection(el, section);
        return;
    }
    if (section.kind === 'divider') {
        setMarkup(el, logic.listDividerMarkup(state.labels));
        return;
    }
    const labels = state.labels;
    el.className = 'list-section section-' + section.key + (section.open ? '' : ' closed');

    const head = el.querySelector('.section-head');
    head.dataset.section = section.key;
    head.setAttribute('aria-expanded', section.open ? 'true' : 'false');
    setText(head.querySelector('.section-label'), labels[section.label] || section.key);
    setText(head.querySelector('.section-count'), section.count == null ? '' : section.count);
    setText(head.querySelector('.section-note'), section.note || '');

    setMarkup(el.querySelector('.section-ranges'), PAST_SECTIONS.has(section.key) && section.open ? historyRangeMarkup() : '');
    setText(el.querySelector('.section-note-line'), section.noteLine || '');

    reconcile(el.querySelector('.section-items'), section.items, rowKey, createItem, updateItem);
}

// Pinned or one of the Desktop groups: a header that folds it, whatever the
// status sections show.
function createGroupSection() {
    const el = document.createElement('section');
    el.className = 'list-section group-section';
    el.innerHTML = '<div class="group-section-head"></div><div class="section-items"></div>';
    return el;
}

function updateGroupSection(el, item) {
    const section = item.section;
    el.className = 'list-section group-section ' + (section.key === 'pinned' ? 'section-pinned' : 'section-group')
        + (section.collapsed ? ' closed' : '');
    // A rewritten header would take the keyboard focus with it, so it goes to
    // the new toggle.
    const head = el.querySelector('.group-section-head');
    const focused = head.contains(document.activeElement);
    setMarkup(head, logic.groupHeaderMarkup(section, state.labels));
    if (focused && !head.contains(document.activeElement) && head.firstElementChild) {
        head.firstElementChild.focus();
    }
    // Folded, `subsections` is empty and the frame shrinks to its header.
    const pinned = section.key === 'pinned';
    reconcile(el.querySelector('.section-items'), item.subsections, (sub) => sub.key, createGroupSub, (subEl, sub) => updateGroupSub(subEl, sub, pinned));
}

// One sub-heading of a group - the list section its sessions would otherwise
// be listed in - with its rows. It folds on its own; folded, its rows are left
// out and its header still gives the count.
function createGroupSub() {
    const el = document.createElement('div');
    el.className = 'group-sub';
    el.innerHTML = '<div class="group-sub-head"></div><div class="group-sub-items"></div>';
    return el;
}

function updateGroupSub(el, sub, pinned) {
    setClass(el, 'group-sub group-sub-' + sub.key + (sub.collapsed ? ' closed' : ''));
    // A rewritten toggle would take the keyboard focus with it.
    const head = el.querySelector('.group-sub-head');
    const focused = head.contains(document.activeElement);
    setMarkup(head, logic.groupSubheadMarkup(sub, state.labels));
    if (focused && !head.contains(document.activeElement) && head.firstElementChild) {
        head.firstElementChild.focus();
    }
    const rows = sub.collapsed ? [] : sub.sessions;
    reconcile(el.querySelector('.group-sub-items'), rows, rowKey, createItem, (itemEl, session) => updateGroupItem(itemEl, session, pinned));
}

// A session that ended stays in its group, dimmed.
function updateGroupItem(el, session, pinned) {
    updateItem(el, session, pinned);
    el.classList.toggle('ended', !session.alive);
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

// How far back the graph reaches, as a row of buttons above it, with the
// buttons that fold every session and the legend's switch.
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
        + esc(state.labels.graph_expand_all || '') + '</button>'
        + '<button' + attr('type', 'button') + attr('class', 'range-btn' + (state.graphLegend ? ' active' : '')) + attr('data-graph-legend', '')
        + attr('aria-pressed', state.graphLegend ? 'true' : 'false') + '>' + esc(state.labels.graph_legend || '') + '</button>';
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
let legendSlot = null;

function ensureShell() {
    if (listSlot && listSlot.isConnected) {
        return;
    }
    const content = document.getElementById('content');
    content.innerHTML = '<div class="section-chips"></div><div class="notice-slot"></div>'
        + '<div class="list-slot"></div><div class="graph-slot"></div><div class="legend-slot"></div><div class="state-slot"></div>';
    chipsSlot = content.querySelector('.section-chips');
    noticeSlot = content.querySelector('.notice-slot');
    listSlot = content.querySelector('.list-slot');
    stateSlot = content.querySelector('.state-slot');
    graphSlot = content.querySelector('.graph-slot');
    legendSlot = content.querySelector('.legend-slot');
}

// The sessions as the list shows them, in display order, with the query's
// meta match on each. Returns what render and the keyboard need.
function render(snapshot) {
    const previous = state.last;
    state.last = snapshot;
    // What ended since the last snapshot stays in the graph for a while; a
    // re-render of the same snapshot finds nothing new.
    state.processTracker = logic.trackEndedProcesses(state.processTracker,
        (snapshot.sessions || []).map((raw) => ({ key: rowKey(raw), processes: logic.processesOf(raw) })),
        Date.now(), (key, pid) => key === state.selectedKey && pid === state.selectedProcess);

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
    const historyActive = historyWanted();
    const historyRows = Array.isArray(state.history)
        ? logic.pruneResumedHistory(visibleHistory(), snapshot.sessions || [])
        : [];
    const foldHistory = (historyActive || widened) && Array.isArray(state.history);
    let rawSessions = snapshot.sessions || [];
    if (foldHistory) {
        rawSessions = rawSessions.concat(historyRows);
    }
    // A live session that just left the snapshot was excluded from the history
    // fetch; fetch again so it moves into "Older" instead of vanishing from
    // both - at once while the rows are shown, else on the next opening, so a
    // closed section never starts a scan of its own.
    if (Array.isArray(state.history) && previous && logic.historyNeedsRefresh(previous.sessions, snapshot.sessions)) {
        if (foldHistory) {
            refreshHistory();
        } else {
            state.historyStale = true;
        }
    }
    const loadingNote = ((historyActive || widened) && state.historyLoading) ? labels.history_loading : '';

    const projects = logic.groupProjects(rawSessions, labels, prices);
    const sessions = logic.flattenProjects(projects);
    attachGitState(sessions);

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
    updateToolbarSummary(snapshot, prices, sessions);
    ensureShell();

    const compact = state.compact;
    const grouping = compact ? 'urgency' : state.group;

    const groupsView = listGroupsView();
    const sectionData = logic.buildSections(found, state.filters, groupsView);

    // Section counts follow the query, like the list, and count only what a
    // status section lists. "Older" and "Archived" count their past sessions
    // from the cache while neither is open, since they are not folded in -
    // built like the list's, so a title match counts there as it shows there.
    const counts = new Map(sectionData.map((section) => [section.key, section.sessions.length]));
    if (!foldHistory) {
        let olderCount = null;
        let archivedPast = null;
        if (Array.isArray(state.history)) {
            const past = searchActive
                ? logic.flattenProjects(logic.groupProjects(historyRows, labels, prices)).filter(matchesQuery)
                : historyRows;
            olderCount = past.filter((row) => logic.sessionBucket(row, groupsView.archived) === 'history').length;
            archivedPast = past.filter((row) => logic.sessionBucket(row, groupsView.archived) === 'archived').length;
        }
        counts.set('older', olderCount);
        const archivedHere = counts.get('archived') || 0;
        counts.set('archived', archivedPast === null && archivedHere === 0 ? null : archivedHere + (archivedPast || 0));
    }
    document.body.classList.toggle('group-urgency', grouping === 'urgency');
    document.body.classList.toggle('group-graph', grouping === 'graph');
    applyListWidth();

    let outsideFilters = 0;
    const shown = [];
    // Sessions listed in a folded group: not on screen, but not missing either.
    let folded = 0;
    if (grouping !== 'graph') {
        setMarkup(graphSlot, '');
        setMarkup(legendSlot, '');
    }
    if (grouping === 'graph') {
        // The live sessions the query leaves, with their subagents and the
        // messages between them; the sections do not apply here.
        // The chip row holds the graph's time window here. Through setMarkup,
        // so its cache sees the slot change and the toggles come back when the
        // Project grouping writes the same markup again.
        setMarkup(chipsSlot, graphRangeMarkup());
        listSlot.replaceChildren();
        // Only the sessions with something inside the graph's time window.
        const graphSeconds = logic.graphRange(state.graphRange).seconds;
        const graphSessions = found.filter((session) => !session.is_history && logic.sessionInGraphWindow(session, graphSeconds));
        shown.push(...graphSessions);
        // An ended process carries how many seconds from now its fade starts.
        const now = Date.now();
        const entries = graphSessions.map((session) => ({
            key: rowKey(session), session: session,
            ended: endedProcessesOf(rowKey(session)).map((process) => Object.assign({}, process, { fade_in: (process.fade_at - now) / 1000 })),
        }));
        const selection = { key: state.selectedKey, agent: state.selectedAgent, process: state.selectedProcess, tool: state.selectedTool };
        const model = logic.graphModel(entries, graphSeconds, selection, state.graphCollapsed);
        setMarkup(graphSlot, graphSessions.length > 0 ? logic.graphMarkup(logic.graphLayout(model), labels, selection) : '');
        setMarkup(legendSlot, graphSessions.length > 0 && state.graphLegend ? logic.graphLegendMarkup(labels) : '');
        fitGraphText(graphSlot);
    } else if (grouping === 'urgency') {
        setMarkup(chipsSlot, '');
        folded = logic.foldedGroupSessionCount(sectionData);
        const sections = [];
        for (const section of sectionData) {
            if (section.kind === 'group') {
                // A folded group still says what it holds in its header. The
                // keyboard walks the rows in the order of the sub-headings.
                const subsections = section.collapsed ? [] : section.subsections;
                for (const sub of subsections) {
                    if (!sub.collapsed) {
                        shown.push(...sub.sessions);
                    }
                }
                sections.push({ key: section.key, kind: 'group', section: section, subsections: subsections });
                continue;
            }
            if (sections.length > 0 && sections[sections.length - 1].kind === 'group') {
                sections.push({ key: '__other', kind: 'divider' });
            }
            const open = section.active;
            const items = open ? section.sessions : section.sessions.filter(isWidenedHit);
            if (!section.active) {
                outsideFilters += items.length;
            }
            shown.push(...items);
            sections.push({
                key: section.key,
                label: section.label,
                open: open,
                items: items,
                count: counts.get(section.key),
                note: sectionNote(section.key, open),
                noteLine: PAST_SECTIONS.has(section.key) && open ? loadingNote : '',
            });
        }
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
                visible.push(logic.withArchivedMark(session, groupsView.archived));
            } else if (isWidenedHit(session)) {
                visible.push(logic.withArchivedMark(session, groupsView.archived));
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
    const listed = shown.length + folded;
    const noteHere = grouping !== 'project' && listed > 0 ? '' : loadingNote;
    const sectionsSpeak = grouping === 'urgency' && !searchActive && !noteHere && sessions.length > 0;
    if ((listed > 0 && !(searchActive && state.searchLoading) && !noteHere) || sectionsSpeak) {
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
    if (!PAST_SECTIONS.has(sectionKey) || open) {
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
    // A tool filter belonged to the session that left; the next was never filtered.
    state.selectedTool = null;
    state.scrollToActivity = false;
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

// The count of active sessions and their cost, at the right of the toolbar,
// and what the warm prompt caches of the sessions waiting on you hold. An ended
// session the snapshot still lists for a while is none of these.
function updateToolbarSummary(snapshot, prices, sessions) {
    const el = document.getElementById('toolbar-summary');
    if (!el) {
        return;
    }
    const live = (snapshot.sessions || []).filter((raw) => raw.alive);
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
    const cached = logic.warmCacheCostUsd(sessions);
    const cache = cached === null ? '' : ' · <span class="summary-cache"' + attr('data-tip', state.labels.summary_cache_tip || '') + '>'
        + esc(state.labels.summary_cache || '').replace('{cost}', () => '<b>' + esc(logic.formatCostCents(cached)) + '</b>') + '</span>';
    setMarkup(el, label + (priced ? ' · <b>' + esc(logic.formatCost(total)) + '</b>' : '') + cache);
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
