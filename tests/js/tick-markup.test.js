'use strict';

/* The ticking duration's markup, as render.js writes it, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('the same running call writes the same markup on the next poll', () => {
    // Its start, rounded from an age and the page's clock, flipped between two
    // seconds near a half second, and the rewrite dropped a text selection.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    // Started at 1000.45 s; two polls whose answers took a different time to arrive.
    const first = vm.runInContext('tickMarkup(20.0, 1020450 + 40)', context);
    const second = vm.runInContext('tickMarkup(25.0, 1025450 + 120)', context);

    assert.strictEqual(second, first);
});
