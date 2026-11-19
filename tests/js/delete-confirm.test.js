'use strict';

/* The delete confirmation's quote of the row, as actions.js builds it, in the
   shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('the confirmation quotes the age the row shows now', () => {
    // The age was the history fetch's, frozen, while the row's kept ticking.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const run = (code) => vm.runInContext(code, context);
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        globalThis.__quoted = null;
        confirmDeleteSession = (id, cwd, name, age) => { __quoted = age; };
        // Fetched an hour ago, when the session was a minute old.
        state.historyReceivedAt = Date.now() - 3600 * 1000;
        runSessionAction('delete', { session_id: 's', pid: 0, cwd: 'c', name: 'n', is_history: true, age_seconds: 60, origin: 'local' });
    `);

    assert.strictEqual(run('__quoted'), run('logic.formatAge(3660, state.labels)'));
});
