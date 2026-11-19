'use strict';

/* Leaving the compact view, as header.js does it, in the shared vm context the
   browser loads the scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('leaving the compact view into the graph asks for the graph\'s data at once', () => {
    // The snapshot carries subagents, messages and tools only while the graph
    // shows; without a fetch it stayed bare until the next full poll.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout, localStorage: { setItem() {} } });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__ticks = 0;
    const run = (code) => vm.runInContext(code, context);
    run("applyCompact = () => {}; render = () => {}; tick = () => { __ticks += 1; }; state.group = 'graph'; state.compact = true;");

    run('toggleCompact(false);');
    assert.strictEqual(context.__ticks, 1);
    run('toggleCompact(true);');
    assert.strictEqual(context.__ticks, 1);
});
