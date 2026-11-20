'use strict';

/* --- the settings dialog ---
   The gear in the toolbar opens a modal that edits the app's settings file.
   The bridge describes the settings (get_settings) and saves them
   (save_settings, which validates again and writes the file); this file owns
   the dialog's DOM and events, logic.js the parsing and the markup. Without a
   bridge (the browser preview) the mock's description stands in, and a save
   only changes that copy. */

// The bridge's description while the dialog is open: specs, the values in
// force, defaults, load errors, languages, window size and the file's path.
let settingsView = null;
// The fields a save was refused for, by key, until each is edited (see refreshSettingsForm).
const settingsSaveErrors = new Map();

function initSettingsButton() {
    const button = document.getElementById('settings-toggle');
    if (button) {
        button.addEventListener('click', () => openSettings());
    }
    refreshSettingsButton();
}

// Called on init and once labels arrive.
function refreshSettingsButton() {
    const button = document.getElementById('settings-toggle');
    const tip = state.labels.settings_open;
    if (button && tip) {
        button.dataset.tip = tip + ' (' + (state.labels.key_ctrl || 'Ctrl') + ' ,)';
        button.setAttribute('aria-label', tip);
    }
}

function settingsOpen() {
    return Boolean(document.getElementById('settings-overlay'));
}

async function fetchSettings() {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_settings === 'function') {
        return bridge.get_settings();
    }
    const mock = window.__MOCK_SETTINGS__;
    return mock ? JSON.parse(JSON.stringify(mock)) : null;
}

async function submitSettings(values) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.save_settings === 'function') {
        return bridge.save_settings(values);
    }
    const mock = window.__MOCK_SETTINGS__;
    if (!mock) {
        return { ok: false, errors: [{ key: '', code: 'write', detail: '' }] };
    }
    const changed = logic.settingsChangedKeys(mock.specs, values, mock.values);
    const restart = changed.filter((key) => mock.specs.some((spec) => spec.key === key && spec.applies === 'restart'));
    mock.values = Object.assign({}, mock.values, values);
    mock.exists = true;
    return { ok: true, values: mock.values, changed, restart, reload: false, path: mock.path };
}

async function openSettings() {
    if (settingsOpen() || document.getElementById('confirm-overlay')) {
        return;
    }
    closeMenu();
    hideTooltip();

    let description = null;
    try {
        description = await fetchSettings();
    } catch (err) {
        description = null;
    }
    if (!description || !Array.isArray(description.specs) || settingsOpen()) {
        if (!description) {
            toast(state.labels.settings_unavailable);
        }
        return;
    }
    settingsView = description;

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'settings-overlay';
    overlay.innerHTML = '<div class="modal settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">'
        + '<div class="settings-head">'
        +     '<div class="modal-title" id="settings-title"></div>'
        +     '<button class="settings-close" type="button" data-act="cancel">'
        +         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
        +         ' aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"></path></svg>'
        +     '</button>'
        + '</div>'
        + '<div class="settings-body"></div>'
        + '<div class="settings-foot">'
        +     '<div class="settings-version"></div>'
        +     '<div class="settings-file"></div>'
        +     '<div class="settings-message" role="alert"></div>'
        +     '<div class="modal-actions">'
        +         '<button class="modal-btn primary" type="button" data-act="save"></button>'
        +         '<button class="modal-btn" type="button" data-act="cancel"></button>'
        +     '</div>'
        + '</div></div>';

    const labels = state.labels;
    overlay.querySelector('.modal-title').textContent = labels.settings_title;
    const close = overlay.querySelector('.settings-close');
    close.dataset.tip = labels.settings_close;
    close.setAttribute('aria-label', labels.settings_close);
    overlay.querySelector('.modal-actions [data-act="cancel"]').textContent = labels.cancel;
    overlay.querySelector('[data-act="save"]').textContent = labels.settings_save;
    overlay.querySelector('.settings-body').innerHTML = logic.settingsFormMarkup(settingsView, labels);
    overlay.querySelector('.settings-file').innerHTML = logic.settingsFileMarkup(settingsView, labels);
    overlay.querySelector('.settings-version').innerHTML = logic.appVersionMarkup(updateStatus, labels);

    overlay.addEventListener('input', onSettingsInput);
    overlay.addEventListener('change', onSettingsInput);
    overlay.addEventListener('click', onSettingsClick);
    document.addEventListener('keydown', onSettingsKey, true);
    document.body.appendChild(overlay);

    refreshSettingsForm();
    const first = overlay.querySelector('[data-key]');
    (first || overlay.querySelector('[data-act="save"]')).focus();
}

function closeSettings() {
    const overlay = document.getElementById('settings-overlay');
    if (overlay) {
        overlay.remove();
    }
    document.removeEventListener('keydown', onSettingsKey, true);
    settingsView = null;
    settingsSaveErrors.clear();
}

// Every field's value as the dialog holds it, and the error of each field that
// does not parse.
function readSettingsForm() {
    const overlay = document.getElementById('settings-overlay');
    const values = {};
    const errors = new Map();
    if (!overlay || !settingsView) {
        return { values, errors };
    }
    for (const spec of settingsView.specs) {
        const input = overlay.querySelector('[data-key="' + CSS.escape(String(spec.key)) + '"]');
        if (!input) {
            continue;
        }
        const raw = spec.kind === 'bool' ? input.checked : input.value;
        const parsed = logic.parseSettingInput(spec, raw, settingsView.languages);
        if (parsed.error) {
            errors.set(spec.key, Object.assign({ key: spec.key }, parsed.error));
        } else {
            values[spec.key] = parsed.value;
        }
    }
    return { values, errors };
}

// Bring every field's error, reset button and duration in line with what it
// holds, and enable Save only for a valid change.
function refreshSettingsForm() {
    const overlay = document.getElementById('settings-overlay');
    if (!overlay || !settingsView) {
        return;
    }
    const labels = state.labels;
    const { values, errors } = readSettingsForm();
    // A field the backend refused speaks for itself until it is edited.
    for (const [key, error] of settingsSaveErrors) {
        if (!errors.has(key)) {
            errors.set(key, error);
        }
    }
    for (const spec of settingsView.specs) {
        const key = CSS.escape(String(spec.key));
        const field = overlay.querySelector('[data-setting="' + key + '"]');
        if (!field) {
            continue;
        }
        const error = errors.get(spec.key);
        field.classList.toggle('invalid', Boolean(error));
        const errorLine = field.querySelector('.setting-error');
        if (error) {
            errorLine.textContent = logic.settingErrorText(error, labels);
        } else if (errorLine.dataset.edited) {
            errorLine.textContent = '';
        }
        const reset = field.querySelector('[data-reset]');
        if (reset) {
            reset.hidden = !error && values[spec.key] === settingsView.defaults[spec.key];
        }
        const duration = field.querySelector('[data-duration-for]');
        if (duration) {
            duration.textContent = error ? '' : logic.settingDurationText(spec, values[spec.key], labels);
        }
    }
    // A file with rejected entries, or one read from an earlier location, is
    // worth saving as it is: the save cleans or moves it.
    const changed = logic.settingsChangedKeys(settingsView.specs, values, settingsView.values);
    const fileNeedsSave = (Array.isArray(settingsView.errors) && settingsView.errors.length > 0) || Boolean(settingsView.legacy_path);
    overlay.querySelector('[data-act="save"]').disabled = errors.size > 0 || (changed.length === 0 && !fileNeedsSave);
}

function onSettingsInput(event) {
    const input = event.target && event.target.closest ? event.target.closest('[data-key]') : null;
    if (!input) {
        return;
    }
    // A load error on this field described the file's value; once the field
    // is edited, the field's own check speaks for it.
    const field = input.closest('.setting');
    const errorLine = field ? field.querySelector('.setting-error') : null;
    if (errorLine) {
        errorLine.dataset.edited = '1';
    }
    settingsSaveErrors.delete(input.dataset.key);
    document.querySelector('#settings-overlay .settings-message').textContent = '';
    refreshSettingsForm();
}

function setSettingField(key, value) {
    const overlay = document.getElementById('settings-overlay');
    const input = overlay ? overlay.querySelector('[data-key="' + CSS.escape(String(key)) + '"]') : null;
    if (!input) {
        return;
    }
    if (input.type === 'checkbox') {
        input.checked = Boolean(value);
    } else {
        input.value = String(value);
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
}

// Put the window's size into the fields, measured now: the window can be
// resized while the dialog is open, and the size read when it opened is stale.
async function useCurrentSize(button) {
    let fresh = null;
    try {
        fresh = await fetchSettings();
    } catch (err) {
        fresh = null;
    }
    if (!settingsOpen() || !settingsView) {
        return;
    }
    if (fresh && logic.validWindowSize(fresh.window_size)) {
        settingsView.window_size = fresh.window_size;
    }
    if (!logic.validWindowSize(settingsView.window_size)) {
        return;
    }
    const [width, height] = settingsView.window_size;
    const label = button.querySelector('.setting-size');
    if (label) {
        label.textContent = width + ' × ' + height;
    }
    setSettingField('window_width', width);
    setSettingField('window_height', height);
}

function onSettingsClick(event) {
    const overlay = document.getElementById('settings-overlay');
    const target = event.target && event.target.closest ? event.target : null;
    if (!overlay || !target || !settingsView) {
        return;
    }
    const act = target.closest('[data-act]');
    if (act) {
        if (act.dataset.act === 'save') {
            saveSettingsForm();
        } else {
            closeSettings();
        }
        return;
    }
    const reset = target.closest('[data-reset]');
    if (reset) {
        const key = reset.dataset.reset;
        if (Object.prototype.hasOwnProperty.call(settingsView.defaults, key)) {
            setSettingField(key, settingsView.defaults[key]);
            const input = overlay.querySelector('[data-key="' + CSS.escape(key) + '"]');
            if (input) {
                input.focus();
            }
        }
        return;
    }
    const useSize = target.closest('[data-use-size]');
    if (useSize) {
        useCurrentSize(useSize);
        return;
    }
    if (target.closest('[data-reveal-settings]')) {
        const bridge = apiBridge();
        if (bridge && typeof bridge.reveal_settings_file === 'function') {
            Promise.resolve(bridge.reveal_settings_file()).catch(() => { /* window closing */ });
        }
    }
}

// The dialog keeps the keys while it is open: Escape cancels, Enter in a field
// saves, and Tab cycles within the dialog rather than into the page behind it.
function onSettingsKey(event) {
    const overlay = document.getElementById('settings-overlay');
    if (!overlay) {
        return;
    }
    if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeSettings();
        return;
    }
    if (event.key === 'Enter' && event.target && event.target.matches && event.target.matches('input[data-key]')) {
        event.preventDefault();
        saveSettingsForm();
        return;
    }
    if (event.key === 'Tab') {
        trapTab(event, overlay);
    }
}

async function saveSettingsForm() {
    const overlay = document.getElementById('settings-overlay');
    if (!overlay || !settingsView) {
        return;
    }
    const saveButton = overlay.querySelector('[data-act="save"]');
    const { values, errors } = readSettingsForm();
    if (errors.size > 0 || saveButton.disabled) {
        return;
    }

    saveButton.disabled = true;
    let result = null;
    try {
        result = await submitSettings(values);
    } catch (err) {
        result = null;
    }
    // A save that went through is applied even if the dialog was closed
    // meanwhile: the file is written and the settings are in force.
    if (result && result.ok) {
        closeSettings();
        applySavedSettings(result);
        return;
    }
    if (!settingsOpen()) {
        return;
    }
    showSettingsErrors(Array.isArray(result && result.errors) ? result.errors : [{ key: '', code: 'write', detail: '' }]);
    refreshSettingsForm();
}

// Put a refused save's errors at their fields; one without a field (a write
// failure, an unknown key) goes to the line above the buttons.
function showSettingsErrors(errors) {
    const overlay = document.getElementById('settings-overlay');
    const labels = state.labels;
    const general = [];
    for (const error of errors) {
        if (!error || typeof error !== 'object') {
            continue;
        }
        const key = typeof error.key === 'string' ? error.key : '';
        const field = key ? overlay.querySelector('[data-setting="' + CSS.escape(key) + '"]') : null;
        if (field) {
            settingsSaveErrors.set(key, error);
        } else {
            general.push(logic.settingErrorText(error, labels));
        }
    }
    overlay.querySelector('.settings-message').textContent = general.join('\n');
}

// What a successful save changes on the page: a new language reloads it (the
// labels come with the bootstrap), a new interval restarts the poll, and every
// other change shows on the next snapshot, fetched at once.
function applySavedSettings(result) {
    const values = result.values && typeof result.values === 'object' ? result.values : {};
    const changed = Array.isArray(result.changed) ? result.changed : [];
    if (result.reload) {
        window.location.reload();
        return;
    }
    if (Number.isSafeInteger(values.poll_interval) && values.poll_interval > 0 && values.poll_interval !== state.pollInterval) {
        state.pollInterval = values.poll_interval;
        startPoll();
    }
    if (typeof values.list_context === 'boolean') {
        state.listContext = values.list_context;
    }
    const restart = Array.isArray(result.restart) ? result.restart : [];
    toast(restart.length > 0 ? state.labels.settings_saved_restart : state.labels.settings_saved);
    if (changed.length > 0) {
        tick();
    }
    if (changed.includes('update_check')) {
        updateStatusLoop();
    }
}

/* --- version and update check ---
   The bridge reports this build's version and, while the update_check setting
   is on, the newest release it has heard of (get_update_status, answered from
   memory - the request itself runs in Python, a few times a day). Asked once a
   minute, every few seconds while a request is under way; the toolbar badge
   shows only for a newer release, the settings dialog names the version always. */

const UPDATE_STATUS_MS = 60 * 1000;
const UPDATE_STATUS_PENDING_MS = 3000;

// The last answer, and the round that may still store one: a restarted loop
// (a saved setting) supersedes a round still waiting on the bridge.
let updateStatus = null;
let updateStatusSeq = 0;
let updateStatusTimer = null;

async function fetchUpdateStatus() {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_update_status === 'function') {
        return bridge.get_update_status();
    }
    return window.__MOCK_UPDATE__ ? Object.assign({}, window.__MOCK_UPDATE__) : null;
}

async function updateStatusLoop() {
    clearTimeout(updateStatusTimer);
    const seq = ++updateStatusSeq;
    let status = updateStatus;
    try {
        status = await fetchUpdateStatus();
    } catch (err) {
        // A bridge hiccup - the next round asks again.
    }
    if (seq !== updateStatusSeq) {
        return;
    }
    updateStatus = status && typeof status === 'object' ? status : null;
    renderUpdateStatus();
    const pending = Boolean(updateStatus && updateStatus.enabled && updateStatus.pending);
    updateStatusTimer = setTimeout(updateStatusLoop, pending ? UPDATE_STATUS_PENDING_MS : UPDATE_STATUS_MS);
}

function renderUpdateStatus() {
    const labels = state.labels;
    const badge = document.getElementById('update-badge');
    if (badge) {
        const update = logic.appUpdateState(updateStatus);
        const show = update.state === 'available' && Boolean(update.url);
        badge.hidden = !show;
        if (show) {
            badge.href = update.url;
            badge.textContent = fmt(labels.update_badge, { version: update.version });
            badge.dataset.tip = fmt(labels.update_badge_tip, { version: update.version, current: updateStatus.current });
        }
    }
    const line = document.querySelector('#settings-overlay .settings-version');
    if (line) {
        line.innerHTML = logic.appVersionMarkup(updateStatus, labels);
    }
}
