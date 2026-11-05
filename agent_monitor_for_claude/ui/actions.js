'use strict';

/* --- actions: select, open, reveal, focus, the session menu, click routing --- */

function openPath(cwd, origin) {
    if (!cwd) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.open_path === 'function') {
        // Fire-and-forget: a Win32-side rejection must not wipe the content area.
        logic.settleCall(() => bridge.open_path(cwd, origin || 'local'));
    }
}

// Show one file in the file manager, selected in its folder. The file is never
// opened - the backend hands it to the shell's select-in-folder call, nothing else.
function revealPath(path, origin) {
    if (!path) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.reveal_path === 'function') {
        logic.settleCall(() => bridge.reveal_path(path, origin || 'local'));
    }
}

// Bring a session's window forward (its editor or terminal).
function focusSession(session) {
    if (!session || !hasWindow(session)) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.focus_session === 'function') {
        logic.settleCall(() => bridge.focus_session(
            Number(session.pid),
            session.project_name || '',
            session.session_id || '',
            Boolean(session.vscode_deeplink),
            session.title || '',
            session.origin || 'local'
        ));
    }
}

// Select a list entry. In the narrow layout `drill` also swaps the list for
// the detail; the wide one shows both anyway.
function selectSession(key, drill) {
    if (!key || !state.sessionsByKey.has(key)) {
        return;
    }
    state.selectedKey = key;
    state.selectedAgent = null;
    state.selectedProcess = null;
    state.selectedTool = null;
    state.scrollToActivity = false;
    if (drill) {
        state.detailOpen = true;
        document.body.classList.add('detail-open');
    }
    markSelectedItem();
    renderDetail();
    refreshGraphFocus();
}

// The graph draws the tool edges of what is selected, so a new selection
// redraws it (a few milliseconds); the list needs nothing of the kind.
function refreshGraphFocus() {
    if (state.group === 'graph' && !state.compact && state.last) {
        render(state.last);
    }
}

// Select one of a session's subagents: the detail describes the agent, and the
// session stays the selection the list and the keyboard work from.
function selectAgent(key, agentId) {
    if (!key || !agentId || !state.sessionsByKey.has(key)) {
        return;
    }
    selectSession(key, true);
    state.selectedAgent = agentId;
    state.selectedTool = null;
    markSelectedItem();
    renderDetail();
    refreshGraphFocus();
}

// Select one of a session's background processes, like a subagent.
function selectProcess(key, pid) {
    if (!key || !Number.isInteger(pid) || pid <= 0 || !state.sessionsByKey.has(key)) {
        return;
    }
    selectSession(key, true);
    state.selectedProcess = pid;
    markSelectedItem();
    renderDetail();
    refreshGraphFocus();
}

// Select a tool under its caller: a subagent's tool narrows its activity to the
// calls of that tool; a session's selects the session, which lists no steps.
function selectTool(key, agentId, toolKey) {
    if (!agentId) {
        selectSession(key, true);
        return;
    }
    selectAgent(key, agentId);
    state.selectedTool = toolKey || null;
    state.scrollToActivity = Boolean(toolKey);
    markSelectedItem();
    renderDetail();
    refreshGraphFocus();
}

function closeDetail() {
    state.detailOpen = false;
    document.body.classList.remove('detail-open');
    const el = findItem(state.selectedKey);
    if (el) {
        el.scrollIntoView({ block: 'nearest' });
    }
}

// Move the selection by `step` entries through the list as shown.
function moveSelection(step) {
    const keys = state.listKeys;
    if (keys.length === 0) {
        return;
    }
    const index = keys.indexOf(state.selectedKey);
    const next = index < 0 ? 0 : Math.max(0, Math.min(keys.length - 1, index + step));
    selectSession(keys[next], false);
    const el = findItem(keys[next]);
    if (el) {
        el.scrollIntoView({ block: 'nearest' });
    }
}

// Run one of a session's actions - from the detail's buttons, the session menu
// or a key.
async function runSessionAction(action, session) {
    if (!session) {
        return;
    }
    const origin = session.origin || 'local';
    if (action === 'open-window') {
        focusSession(session);
    } else if (action === 'copy-id') {
        copyToClipboard(session.session_id || '');
    } else if (action === 'copy-attach') {
        if (session.job_id) {
            copyToClipboard('claude attach ' + session.job_id);
        }
    } else if (action === 'transcript') {
        revealPath(await transcriptPath(session.session_id || '', session.cwd || '', origin), origin);
    } else if (action === 'scratchpad') {
        openPath(await scratchpadPath(session.session_id || '', session.cwd || '', origin), origin);
    } else if (action === 'delete') {
        if (session.is_history) {
            const age = session.age_seconds == null ? '' : logic.formatAge(session.age_seconds, state.labels);
            confirmDeleteSession(session.session_id, session.cwd || '', session.name || '', age, origin);
        }
    } else if (action === 'details') {
        if (state.compact) {
            toggleCompact(false);
        }
        selectSession(rowKey(session), true);
    }
}

// The session menu on a right-click. The transcript and scratchpad entries are
// added only when the session has those files - checked on demand here, never
// on the poll. Only the newest right-click opens its menu: an earlier one whose
// lookups answer last must not open the menu of the entry clicked before.
let sessionMenuRequest = 0;

async function openSessionMenu(itemEl, point) {
    const key = itemEl.dataset.key;
    const session = state.sessionsByKey.get(key);
    if (!session) {
        return;
    }
    const request = ++sessionMenuRequest;
    const origin = session.origin || 'local';
    const [transcript, scratchpad] = await Promise.all([
        transcriptPath(session.session_id || '', session.cwd || '', origin),
        scratchpadPath(session.session_id || '', session.cwd || '', origin),
    ]);
    if (!itemEl.isConnected || request !== sessionMenuRequest) {
        return;
    }

    const labels = state.labels;
    const items = [];
    if (state.compact) {
        items.push({ key: 'details', label: labels.action_show_details });
    }
    items.push({ key: 'copy-id', label: labels.copy_session_id });
    if (session.job_id) {
        items.push({ key: 'copy-attach', label: labels.copy_attach_command });
    }
    if (transcript) {
        items.push({ key: 'transcript', label: labels.show_transcript });
    }
    if (scratchpad) {
        items.push({ key: 'scratchpad', label: labels.show_scratchpad });
    }
    if (session.is_history) {
        items.push({ key: 'delete', label: labels.delete_session, danger: true });
    }

    openMenu(itemEl, items, (action) => runSessionAction(action, session), { type: 'row', rowKey: key, point: point });
}

// Return the session's scratchpad path when one exists, else '' (no entry).
async function scratchpadPath(sessionId, cwd, origin) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.scratchpad_path === 'function') {
        try {
            return (await bridge.scratchpad_path(sessionId, cwd, origin || 'local')) || '';
        } catch (e) {
            return '';
        }
    }
    const mock = window.__MOCK_SCRATCHPADS__;
    return (mock && mock[sessionId]) ? mock[sessionId] : '';
}

// Return the session's transcript file when it exists, else '' (no entry) - a
// session that has not been prompted yet has no transcript on disk.
async function transcriptPath(sessionId, cwd, origin) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.transcript_path === 'function') {
        try {
            return (await bridge.transcript_path(sessionId, cwd, origin || 'local')) || '';
        } catch (e) {
            return '';
        }
    }
    const mock = window.__MOCK_TRANSCRIPTS__;
    return (mock && mock[sessionId]) ? mock[sessionId] : '';
}

/* --- dragging the graph: press anywhere on it and move to pan it both ways ---

   The graph scrolls sideways in its slot and up and down with the list pane;
   a drag moves both. A press that moves less than GRAPH_DRAG_THRESHOLD pixels
   stays a click, so a node still selects. */
const GRAPH_DRAG_THRESHOLD = 4;
let graphDrag = null;
let graphDragClick = false;

function onGraphPointerDown(event) {
    graphDragClick = false;
    const slot = event.button === 0 ? event.target.closest('.graph-slot') : null;
    if (!slot) {
        return;
    }
    const content = document.getElementById('content');
    graphDrag = { slot: slot, content: content, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
        left: slot.scrollLeft, top: content.scrollTop, moved: false };
}

function onGraphPointerMove(event) {
    const drag = graphDrag;
    if (!drag || event.pointerId !== drag.pointerId) {
        return;
    }
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved) {
        if (Math.hypot(dx, dy) < GRAPH_DRAG_THRESHOLD) {
            return;
        }
        drag.moved = true;
        drag.slot.classList.add('dragging');
        try {
            drag.slot.setPointerCapture(drag.pointerId);
        } catch (e) { /* the pointer is gone already */ }
    }
    drag.slot.scrollLeft = drag.left - dx;
    drag.content.scrollTop = drag.top - dy;
    event.preventDefault();
}

function onGraphPointerUp(event) {
    const drag = graphDrag;
    if (!drag || event.pointerId !== drag.pointerId) {
        return;
    }
    graphDrag = null;
    if (drag.moved) {
        drag.slot.classList.remove('dragging');
        graphDragClick = true;
    }
}

/* --- the splitter between the list and the detail ---

   Dragging it sets the list's width, which the detail gives up; a double-click
   or Home gives the default back, the arrow keys move it a step. One width per
   grouping (the graph wants more room than the list), remembered as
   amc-list-width. CSS keeps the detail at its minimum however narrow the
   window becomes. */
const LIST_MIN_WIDTH = 260;
const DETAIL_MIN_WIDTH = 320;
const SPLITTER_KEY_STEP = 24;
let splitterDrag = null;

function listWidthKey() {
    return state.group === 'graph' && !state.compact ? 'graph' : 'list';
}

function loadListWidths() {
    try {
        const stored = JSON.parse(localStorage.getItem('amc-list-width') || '{}');
        const widths = {};
        for (const key of ['list', 'graph']) {
            if (Number.isFinite(stored[key]) && stored[key] > 0) {
                widths[key] = stored[key];
            }
        }
        return widths;
    } catch (e) {
        return {};
    }
}

// Apply the current grouping's width, or the layout's default without one.
function applyListWidth() {
    const width = (state.listWidths || {})[listWidthKey()];
    const workspace = document.getElementById('workspace');
    if (!workspace) {
        return;
    }
    // Set here rather than once, since the labels can arrive after the start.
    const splitter = document.getElementById('splitter');
    if (splitter) {
        splitter.setAttribute('aria-label', state.labels.splitter_tip || '');
    }
    document.body.classList.toggle('list-sized', Number.isFinite(width));
    if (Number.isFinite(width)) {
        workspace.style.setProperty('--list-width', Math.round(width) + 'px');
    } else {
        workspace.style.removeProperty('--list-width');
    }
}

function setListWidth(width, persist) {
    const workspace = document.getElementById('workspace');
    const widths = Object.assign({}, state.listWidths || {});
    if (width === null) {
        delete widths[listWidthKey()];
    } else {
        const max = Math.max(LIST_MIN_WIDTH, workspace.clientWidth - DETAIL_MIN_WIDTH);
        widths[listWidthKey()] = Math.min(max, Math.max(LIST_MIN_WIDTH, width));
    }
    state.listWidths = widths;
    applyListWidth();
    if (persist) {
        try {
            localStorage.setItem('amc-list-width', JSON.stringify(widths));
        } catch (e) { /* storage unavailable */ }
    }
}

function initSplitter() {
    state.listWidths = loadListWidths();
    applyListWidth();
    const splitter = document.getElementById('splitter');
    if (!splitter) {
        return;
    }
    splitter.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) {
            return;
        }
        event.preventDefault();
        const content = document.getElementById('content');
        splitterDrag = { pointerId: event.pointerId, startX: event.clientX, startWidth: content.getBoundingClientRect().width };
        splitter.setPointerCapture(event.pointerId);
        splitter.classList.add('dragging');
        document.body.classList.add('splitter-dragging');
    });
    splitter.addEventListener('pointermove', (event) => {
        if (splitterDrag && event.pointerId === splitterDrag.pointerId) {
            setListWidth(splitterDrag.startWidth + event.clientX - splitterDrag.startX, false);
        }
    });
    const end = (event) => {
        if (!splitterDrag || event.pointerId !== splitterDrag.pointerId) {
            return;
        }
        splitterDrag = null;
        splitter.classList.remove('dragging');
        document.body.classList.remove('splitter-dragging');
        setListWidth((state.listWidths || {})[listWidthKey()], true);
    };
    splitter.addEventListener('pointerup', end);
    splitter.addEventListener('pointercancel', end);
    splitter.addEventListener('dblclick', () => setListWidth(null, true));
    splitter.addEventListener('keydown', (event) => {
        const current = document.getElementById('content').getBoundingClientRect().width;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault();
            event.stopPropagation();
            setListWidth(current + (event.key === 'ArrowLeft' ? -SPLITTER_KEY_STEP : SPLITTER_KEY_STEP), true);
        } else if (event.key === 'Home') {
            event.preventDefault();
            setListWidth(null, true);
        }
    });
}

/* --- click routing: one delegated handler per pane --- */

function onContentClick(event) {
    // The click that ends a drag of the graph selects nothing.
    if (graphDragClick) {
        graphDragClick = false;
        return;
    }
    // The widened search's offer and its way back sit in slots of their own.
    const widen = event.target.closest('.empty-action');
    if (widen) {
        widenSearch(widen.dataset.widen);
        return;
    }
    if (event.target.closest('.notice-reset')) {
        resetWidenedSearch();
        return;
    }

    const graphRange = event.target.closest('[data-graph-range]');
    if (graphRange) {
        setGraphRange(graphRange.dataset.graphRange);
        return;
    }

    const range = event.target.closest('.range-btn');
    if (range) {
        setHistoryRange(range.dataset.range);
        return;
    }

    const section = event.target.closest('.section-head, .section-chip');
    if (section) {
        toggleSection(section.dataset.section);
        return;
    }

    // The path opens the folder; checked before the group head it sits under.
    const pathOpen = event.target.closest('.path-open');
    if (pathOpen) {
        openPath(pathOpen.dataset.cwd, pathOpen.dataset.origin);
        return;
    }

    const groupHead = event.target.closest('.group-head');
    if (groupHead) {
        toggleCollapse(groupHead.dataset.groupKey);
        return;
    }

    // The compact view's switch to this session's details in the full view.
    const openDetails = event.target.closest('.item-open');
    if (openDetails) {
        const item = openDetails.closest('.item');
        runSessionAction('details', item ? state.sessionsByKey.get(item.dataset.key) : null);
        return;
    }

    // The found passages are there to be read and copied.
    if (event.target.closest('.row-hits')) {
        return;
    }

    // A session in the graph selects like an entry of the list; a subagent
    // selects itself, so the detail describes it; a tool lists who called it.
    const toolNode = event.target.closest('.gnode[data-tool]');
    if (toolNode) {
        selectTool(toolNode.dataset.key, toolNode.dataset.agent || null, toolNode.dataset.tool);
        return;
    }
    const graphNode = event.target.closest('.gnode[data-key]');
    if (graphNode) {
        if (graphNode.dataset.agent) {
            selectAgent(graphNode.dataset.key, graphNode.dataset.agent);
        } else if (graphNode.dataset.process) {
            selectProcess(graphNode.dataset.key, Number(graphNode.dataset.process));
        } else {
            selectSession(graphNode.dataset.key, true);
        }
        return;
    }

    const item = event.target.closest('.item');
    if (!item) {
        return;
    }
    const session = state.sessionsByKey.get(item.dataset.key);
    // In the compact view a click jumps to the session's window; one without a window
    // opens its details instead.
    if (state.compact) {
        if (session && hasWindow(session)) {
            focusSession(session);
        } else {
            runSessionAction('details', session);
        }
        return;
    }
    selectSession(item.dataset.key, true);
}

function onContentDoubleClick(event) {
    const graphNode = event.target.closest('.gnode[data-key]');
    if (graphNode) {
        focusSession(state.sessionsByKey.get(graphNode.dataset.key));
        return;
    }
    const item = event.target.closest('.item');
    if (!item || event.target.closest('.row-hits, .item-open')) {
        return;
    }
    const session = state.sessionsByKey.get(item.dataset.key);
    if (state.compact) {
        runSessionAction('details', session);
    } else {
        focusSession(session);
    }
}

function onContentContextMenu(event) {
    const item = event.target.closest('.item');
    if (!item) {
        return;
    }
    event.preventDefault();
    if (!state.compact) {
        selectSession(item.dataset.key, false);
    }
    openSessionMenu(item, { x: event.clientX, y: event.clientY });
}

function onDetailClick(event) {
    // A row of the process table selects that process.
    const processRow = event.target.closest('[data-proc-pid]');
    if (processRow) {
        selectProcess(state.selectedKey, Number(processRow.dataset.procPid));
        return;
    }
    const pathOpen = event.target.closest('.path-open');
    if (pathOpen) {
        openPath(pathOpen.dataset.cwd, pathOpen.dataset.origin);
        return;
    }
    const button = event.target.closest('[data-action]');
    if (!button) {
        return;
    }
    if (button.dataset.action === 'back') {
        closeDetail();
        return;
    }
    if (button.dataset.action === 'markdown') {
        setMarkdown(button.dataset.mode === 'formatted');
        return;
    }
    // From an agent's details: back to its session, or on to the agent that started it.
    if (button.dataset.action === 'agent-session') {
        selectSession(state.selectedKey, true);
        return;
    }
    if (button.dataset.action === 'agent-parent') {
        selectAgent(state.selectedKey, button.dataset.agent);
        return;
    }
    if (button.dataset.action === 'step-filter-clear') {
        state.selectedTool = null;
        markSelectedItem();
        renderDetail();
        refreshGraphFocus();
        return;
    }
    if (button.dataset.action === 'process-parent') {
        selectProcess(state.selectedKey, Number(button.dataset.process));
        return;
    }
    runSessionAction(button.dataset.action, selectedSession());
}
