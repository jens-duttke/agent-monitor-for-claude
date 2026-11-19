'use strict';

/* How a graph label is fitted to its box, as render.js does it, in the shared
   vm context the browser loads the scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('a label that fits keeps an ellipsis of its own, and with it its tooltip', () => {
    // The strip meant for the clipped label's ellipsis also took a title's own.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    vm.runInContext('graphTextMeasurer = () => (value) => value.length * 5;', context);
    const node = { tip: 'Wait for the build…', removeAttribute() { this.tip = null; } };
    const text = { textContent: 'Wait for the build…', classList: { contains: (name) => name === 'gnode-label' }, closest: () => node };
    context.__text = text;

    vm.runInContext('fitOneGraphText(__text); dropRedundantGraphTip(__text);', context);

    assert.strictEqual(text.textContent, 'Wait for the build…');
    assert.strictEqual(node.tip, 'Wait for the build…');
});
