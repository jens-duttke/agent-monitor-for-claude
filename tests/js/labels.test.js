'use strict';

/* The built-in English labels the page falls back to - in the browser preview,
   and in the app while the locale file has not loaded - must cover every label
   the page reads, or that text renders empty. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const UI_DIR = path.join(ROOT, 'agent_monitor_for_claude', 'ui');

test('every locale key the page reads has a built-in default', () => {
    const context = vm.createContext({ window: { addEventListener: () => {} } });
    for (const name of ['logic.js', 'core.js']) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const defaults = vm.runInContext('DEFAULT_LABELS', context);
    const english = JSON.parse(fs.readFileSync(path.join(ROOT, 'locale', 'en.json'), 'utf8'));
    const scripts = fs.readdirSync(UI_DIR)
        .filter((name) => name.endsWith('.js') && name !== 'dev-mock.js')
        .map((name) => fs.readFileSync(path.join(UI_DIR, name), 'utf8'))
        .join('\n');

    const missing = Object.keys(english).filter((key) => !(key in defaults) && new RegExp('\\b' + key + '\\b').test(scripts));

    assert.deepStrictEqual(missing, []);
});
