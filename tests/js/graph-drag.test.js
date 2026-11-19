'use strict';

/* The click a graph pan swallows, as actions.js decides it, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js). */

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

    return (code) => vm.runInContext(code, context);
}

const SLOT = "{ classList: { remove() {} } }";

test('a cancelled pan swallows no later click', () => {
    // A cancelled gesture produces no click to swallow; the flag it left
    // discarded the next keyboard activation instead.
    const run = controller();
    run(`graphDrag = { pointerId: 1, moved: true, slot: ${SLOT} }; onGraphPointerUp({ type: 'pointercancel', pointerId: 1 });`);
    assert.strictEqual(run('graphDragClick'), false);
});

test('a keyboard click is never the end of a pan', () => {
    const run = controller();
    run(`graphDrag = { pointerId: 1, moved: true, slot: ${SLOT} }; onGraphPointerUp({ type: 'pointerup', pointerId: 1 });`);
    assert.strictEqual(run('graphDragClick'), true);
    // The handler reads the target once it is past the swallow.
    run('globalThis.__looked = 0; globalThis.__target = { closest: () => { __looked += 1; return null; } };');
    try {
        run("onContentClick({ detail: 0, target: __target, preventDefault() {}, stopPropagation() {} });");
    } catch (err) {
        // Past the swallow it goes on into the page, which is not here.
    }
    assert.ok(run('__looked') > 0);
    assert.strictEqual(run('graphDragClick'), false);
});
