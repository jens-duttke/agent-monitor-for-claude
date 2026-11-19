'use strict';

/* "Use current size" in the settings dialog, as settings.js handles it, in the
   shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('the size is measured when the button is pressed, not when the dialog opened', async () => {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const label = { textContent: '800 × 600' };
    const button = { querySelector: () => label };
    const target = { closest: (selector) => (selector === '[data-use-size]' ? button : null) };
    context.__target = target;
    context.__set = {};
    context.document = { getElementById: (id) => (id === 'settings-overlay' ? {} : null) };
    const run = (code) => vm.runInContext(code, context);
    run(`
        settingsView = { window_size: [800, 600], defaults: {} };
        fetchSettings = () => Promise.resolve({ window_size: [1200, 900] });
        setSettingField = (key, value) => { __set[key] = value; };
    `);

    // The window was resized while the dialog was open.
    run('onSettingsClick({ target: __target });');
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepStrictEqual(Object.assign({}, context.__set), { window_width: 1200, window_height: 900 });
    assert.strictEqual(label.textContent, '1200 × 900');
});
