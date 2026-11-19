'use strict';

/* A right-click on a list row, as actions.js handles it, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller(selected) {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__menus = 0;
    context.__selected = selected;
    vm.runInContext(`
        selectedTextAt = () => __selected;
        selectSession = () => {};
        openSessionMenu = () => { __menus += 1; };
    `, context);

    return { context, run: (code) => vm.runInContext(code, context) };
}

const EVENT = "{ target: { closest: () => ({ dataset: { key: 's#1' } }) }, clientX: 5, clientY: 5, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }";

test('a right-click on text selected in a row leaves it to the copy menu', () => {
    // The session menu took the event, so a selected excerpt could only be
    // copied with Ctrl+C.
    const { context, run } = controller('the fence wait');
    const prevented = run(`(() => { const event = ${EVENT}; onContentContextMenu(event); return event.defaultPrevented; })()`);
    assert.strictEqual(prevented, false);
    assert.strictEqual(context.__menus, 0);
});

test('a right-click on a row without a selection opens the session menu', () => {
    const { context, run } = controller('');
    run(`onContentContextMenu(${EVENT});`);
    assert.strictEqual(context.__menus, 1);
});
