'use strict';

/* The Desktop groups loop in the shared vm context: it renders once on a
   changed answer and not on an identical one, and its members are kept ids. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout: () => 0, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__renders = 0;

    return { context, run: (code) => vm.runInContext(code, context) };
}

test('the loop renders once on a changed answer and not on an identical one', async () => {
    const { context, run } = controller();
    run(`
        render = () => { __renders += 1; };
        state.last = { sessions: [] };
        __answer = { groups: [{ id: 'g', name: 'N', order: 0, collapsed: false }], members: { s1: 'g' }, archived: [] };
        apiBridge = () => ({ get_desktop_groups: () => __answer });
    `);
    await run('refreshDesktopGroups()');
    assert.equal(context.__renders, 1);
    await run('refreshDesktopGroups()');
    assert.equal(context.__renders, 1);
    run(`__answer = { groups: [{ id: 'g', name: 'N', order: 0, collapsed: false }], members: { s1: 'g', s2: 'g' }, archived: [] }`);
    await run('refreshDesktopGroups()');
    assert.equal(context.__renders, 2);
});

test('group members are kept session ids', async () => {
    const { run } = controller();
    run(`
        render = () => {};
        apiBridge = () => ({ get_desktop_groups: () => ({ groups: [{ id: 'g', name: 'N', order: 0, collapsed: false }], members: { s1: 'g' }, archived: [] }) });
    `);
    await run('refreshDesktopGroups()');

    assert.deepStrictEqual(JSON.parse(JSON.stringify(run('keptSessionIds()'))), ['s1']);
});

test('a bridge failure keeps the previous answer', async () => {
    const { context, run } = controller();
    run(`
        render = () => { __renders += 1; };
        apiBridge = () => ({ get_desktop_groups: () => { throw new Error('x'); } });
    `);
    await run('desktopGroupsLoop()');
    assert.equal(context.__renders, 0);
});
