'use strict';

/* The kept session ids ride along on both the snapshot and the fingerprint
   call, in the shared vm context the browser loads the scripts into. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__calls = [];

    return { context, run: (code) => vm.runInContext(code, context) };
}

test('the snapshot call passes the graph flag and the kept ids', async () => {
    const { context, run } = controller();
    run(`
        keptSessionIds = () => ['kept-1'];
        apiBridge = () => ({ get_snapshot: (graph, keep) => { __calls.push(['snapshot', graph, keep]); return { sessions: [] }; } });
        state.group = 'project';
    `);
    await run('callSnapshot()');

    assert.deepStrictEqual(JSON.parse(JSON.stringify(context.__calls)), [['snapshot', false, ['kept-1']]]);
});

test('the fingerprint call passes the kept ids', async () => {
    const { context, run } = controller();
    run(`
        keptSessionIds = () => ['kept-1'];
        tick = () => Promise.resolve();
        rescanForNewMatches = () => {};
        apiBridge = () => ({ get_fingerprint: (keep) => { __calls.push(['fingerprint', keep]); return 'f1'; } });
    `);
    await run('checkForChanges()');

    assert.deepStrictEqual(JSON.parse(JSON.stringify(context.__calls)), [['fingerprint', ['kept-1']]]);
});

test('without pins or groups there is nothing to keep', () => {
    const { run } = controller();

    assert.deepStrictEqual(JSON.parse(JSON.stringify(run('keptSessionIds()'))), []);
});
