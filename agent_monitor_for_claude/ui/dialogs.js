'use strict';

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
function confirmDeleteSession(sessionId, cwd, name, age, origin) {
    if (!sessionId) {
        return;
    }

    // The dialog quotes the row back, so it confirms *which* session is going -
    // not just that a deletion was intended. A generic prompt cannot catch the
    // one mistake that matters here: the menu of the wrong, similar-looking row.
    const labels = state.labels;
    const target = [name, age].filter(Boolean).join(' · ');
    showConfirm(labels.delete_confirm_title, labels.delete_confirm_body, target, labels.delete_confirm_ok, async () => {
        const bridge = apiBridge();
        if (!bridge || typeof bridge.delete_session !== 'function') {
            return;
        }

        let ok = false;
        try {
            ok = await bridge.delete_session(sessionId, cwd, origin || 'local');
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
// or a backdrop click cancels; only the primary button runs onConfirm. The
// optional detail line names what the action targets and is dropped when empty.
function showConfirm(title, body, detail, okLabel, onConfirm) {
    closeConfirm();

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'confirm-overlay';
    overlay.innerHTML = '<div class="modal" role="dialog" aria-modal="true">'
        + '<div class="modal-title"></div>'
        + '<div class="modal-detail"></div>'
        + '<div class="modal-body"></div>'
        + '<div class="modal-actions">'
        +     '<button class="modal-btn" type="button" data-act="cancel"></button>'
        +     '<button class="modal-btn danger" type="button" data-act="ok"></button>'
        + '</div></div>';

    overlay.querySelector('.modal-title').textContent = title || '';
    overlay.querySelector('.modal-detail').textContent = detail || '';
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
    // The destructive button is never the one under the initial focus: a dialog
    // that appears is often dismissed with a reflex Enter or Space, and that key
    // must cancel, never delete.
    cancelBtn.focus();
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
