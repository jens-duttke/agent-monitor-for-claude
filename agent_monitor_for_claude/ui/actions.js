'use strict';

/* --- row and panel actions: open, reveal, focus, row menu, click routing --- */

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

// Show one file in the file manager, selected in its folder. The file is never opened -
// the backend hands it to the shell's select-in-folder call, nothing else.
function revealPath(path, origin) {
    if (!path) {
        return;
    }
    const bridge = apiBridge();
    if (bridge && typeof bridge.reveal_path === 'function') {
        // Fire-and-forget: a Win32-side rejection must not wipe the content area.
        logic.settleCall(() => bridge.reveal_path(path, origin || 'local'));
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
            el.dataset.title || '',
            el.dataset.origin || 'local'
        ));
    }
}

// Build and open a row's action menu. The transcript and scratchpad entries are
// added only when that session actually has those files - checked on demand here
// (two quick bridge stats, run together), never on the per-second poll.
async function openRowMenu(menuBtn) {
    const sessionId = menuBtn.dataset.session || '';
    const cwd = menuBtn.dataset.cwd || '';
    const origin = menuBtn.dataset.origin || 'local';
    const rowEl = menuBtn.closest('.row');
    const menuRowKey = rowEl ? rowEl.dataset.key : '';

    const [transcript, scratchpad] = await Promise.all([
        transcriptPath(sessionId, cwd, origin),
        scratchpadPath(sessionId, cwd, origin),
    ]);

    // The row could have been reconciled away while the check was in flight.
    if (!menuBtn.isConnected) {
        return;
    }

    const items = [{ key: 'copy-id', label: state.labels.copy_session_id }];
    const jobId = menuBtn.dataset.job || '';
    if (jobId) {
        items.push({ key: 'copy-attach', label: state.labels.copy_attach_command });
    }
    if (transcript) {
        items.push({ key: 'transcript', label: state.labels.show_transcript });
    }
    if (scratchpad) {
        items.push({ key: 'scratchpad', label: state.labels.show_scratchpad });
    }
    if (menuBtn.dataset.history === '1') {
        items.push({ key: 'delete', label: state.labels.delete_session, danger: true });
    }

    openMenu(menuBtn, items, (key) => {
        if (key === 'copy-id') {
            copyToClipboard(sessionId);
        } else if (key === 'copy-attach') {
            copyToClipboard('claude attach ' + jobId);
        } else if (key === 'transcript') {
            revealPath(transcript, origin);
        } else if (key === 'scratchpad') {
            openPath(scratchpad, origin);
        } else if (key === 'delete') {
            confirmDeleteSession(sessionId, cwd, rowCellText(rowEl, '.name'), rowCellText(rowEl, '.age'), origin);
        }
    }, { type: 'row', rowKey: menuRowKey });
}

// The rendered text of one of a row's cells - read at click time, so a dialog
// quotes exactly what the user saw (a live-ticking age included).
function rowCellText(rowEl, selector) {
    const cell = rowEl ? rowEl.querySelector(selector) : null;
    return cell ? cell.textContent.trim() : '';
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

// One delegated handler for the whole content area, so reconciled rows never
// need per-node listeners rebound on every render.
function onContentClick(event) {
    // The two controls of the widened search - the offer to look wider, and the
    // way back out of it. Checked first: both sit in slots of their own, where
    // no row or panel handler below could claim the click.
    const widen = event.target.closest('.empty-action');
    if (widen) {
        widenSearch(widen.dataset.widen);
        return;
    }

    if (event.target.closest('.notice-reset')) {
        resetWidenedSearch();
        return;
    }

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
        openPath(pathHead ? pathHead.dataset.cwd : '', pathHead ? pathHead.dataset.origin : 'local');
        return;
    }

    const head = event.target.closest('.panel-head');
    if (head) {
        toggleCollapse(head.dataset.key);
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

    // The hover-only info pills (the model and CLI-version "+N" histories, the
    // running-subagent, worktree and Remote Control badges) are tooltip triggers
    // with a help cursor, not navigation targets - a click on one must not fall
    // through to focusing the session's window. The changelog link is a target of its own:
    // it opens in the browser, so it must not also raise the agent's window.
    if (event.target.closest('.model-more, .cli-more, .cli-link, .agents-badge, .worktree-badge, .rc-badge')) {
        return;
    }

    // The found passages are there to be read and copied, so a click that lands
    // in them must not raise the session's window out from under a selection.
    if (event.target.closest('.row-hits')) {
        return;
    }

    const focusEl = event.target.closest('.row[data-pid], .hero-link[data-pid]');
    if (focusEl) {
        focusSession(focusEl);
    }
}
