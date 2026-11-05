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
    if (drill) {
        state.detailOpen = true;
        document.body.classList.add('detail-open');
    }
    markSelectedItem();
    renderDetail();
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

/* --- click routing: one delegated handler per pane --- */

function onContentClick(event) {
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

    // A session in the graph selects like an entry of the list.
    const graphNode = event.target.closest('.gnode[data-key]');
    if (graphNode) {
        selectSession(graphNode.dataset.key, true);
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
    runSessionAction(button.dataset.action, selectedSession());
}
