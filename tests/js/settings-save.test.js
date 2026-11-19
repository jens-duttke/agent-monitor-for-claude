'use strict';

/* Saving the settings dialog as settings.js does it, in the shared vm context
   the browser loads the scripts into (see global-scope.test.js), on a stand-in
   dialog with one field. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout, CSS: { escape: (text) => text } });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const classes = new Set();
    const errorLine = { textContent: '', dataset: {} };
    const field = {
        classList: { add: (name) => classes.add(name), toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) },
        querySelector: (selector) => (selector === '.setting-error' ? errorLine : null),
    };
    const save = { disabled: false };
    const message = { textContent: '' };
    const overlay = {
        remove() { context.__open = false; },
        querySelector: (selector) => {
            if (selector.startsWith('[data-setting=')) return field;
            if (selector === '[data-act="save"]') return save;
            if (selector === '.settings-message') return message;
            return null;
        },
    };
    context.__open = true;
    context.document = { getElementById: (id) => (id === 'settings-overlay' && context.__open ? overlay : null), removeEventListener() {} };
    context.__applied = 0;
    vm.runInContext(`
        settingsView = { specs: [{ key: 'poll_interval' }], values: { poll_interval: 1 }, defaults: { poll_interval: 1 }, errors: [] };
        readSettingsForm = () => ({ values: { poll_interval: 2 }, errors: new Map() });
        applySavedSettings = () => { __applied += 1; };
    `, context);

    return { context, classes, save, run: (code) => vm.runInContext(code, context) };
}

test('a save the dialog was closed during still takes effect', async () => {
    // Escape right after Enter: the file was written and the settings put in
    // force, but the page never applied them - no reload, no new interval.
    const { context, run } = controller();
    run("submitSettings = () => new Promise((resolve) => { globalThis.__resolve = resolve; });");
    const saving = run('saveSettingsForm()');
    run('closeSettings();');
    run("__resolve({ ok: true, changed: ['poll_interval'] });");
    await saving;
    assert.strictEqual(context.__applied, 1);
});

test('a field the backend refused stays marked until it is edited', async () => {
    const { classes, save, run } = controller();
    run("submitSettings = () => Promise.resolve({ ok: false, errors: [{ key: 'poll_interval', code: 'maximum', limit: 1 }] });");
    await run('saveSettingsForm()');
    assert.ok(classes.has('invalid'));
    assert.strictEqual(save.disabled, true);
});
