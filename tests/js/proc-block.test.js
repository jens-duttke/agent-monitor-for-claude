'use strict';

/* The processes block of the details, as proc-panel.js keeps it, in the
   shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('the block follows the directory its session was filed under', () => {
    // A worktree session's directory is rewritten to its launch project once
    // the switch is read; the block kept asking for tasks under the old one.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout: () => 1, clearTimeout: () => {} });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const run = (code) => vm.runInContext(code, context);
    run(`
        procBlockLoop = () => {};
        globalThis.__el = { innerHTML: '', querySelectorAll: () => [{}, {}, {}, {}], querySelector: () => ({ textContent: '' }) };
        showProcBlock(__el, null, { session_id: 's', pid: 1, cwd: 'D:/proj-worktrees/feature', origin: 'local' });
        showProcBlock(__el, null, { session_id: 's', pid: 1, cwd: 'D:/proj', origin: 'local' });
    `);

    assert.strictEqual(run('procBlock.cwd'), 'D:/proj');
});
