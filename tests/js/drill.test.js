'use strict';

/* The delayed drill into a row in the narrow layout, as actions.js schedules
   it, in the shared vm context the browser loads the scripts into (see
   global-scope.test.js), with the timer run by the test. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const timers = [];
    const window = { addEventListener: () => {}, matchMedia: () => ({ matches: true }) };
    const context = vm.createContext({ window, setTimeout: (fn) => timers.push(fn), clearTimeout: () => {} });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__selected = [];
    vm.runInContext('selectSession = (key, drill) => { state.selectedKey = key; __selected.push([key, drill]); };', context);

    return { timers, context, run: (code) => vm.runInContext(code, context) };
}

test('a drill scheduled for a row the selection has left is dropped', () => {
    const { timers, context, run } = controller();
    run("state.listKeys = ['a#1', 'b#2']; selectEntry('a#1');");
    // ArrowDown, or a poll, moves on before the delay ran out.
    run("state.selectedKey = 'b#2';");
    timers.forEach((fn) => fn());

    assert.deepStrictEqual(Array.from(context.__selected, (pair) => Array.from(pair)), [['a#1', false]]);
});
