'use strict';

/* The confirmation dialog's keys, as dialogs.js handles them, in the shared vm
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
    context.document = { getElementById: () => null, removeEventListener: () => {} };

    return (code) => vm.runInContext(code, context);
}

test('Escape that cancels the confirmation goes no further', () => {
    // Left to bubble, the page's own Escape also closed the details behind it.
    const run = controller();
    const event = run(`(() => {
        const event = { key: 'Escape', prevented: false, stopped: false,
            preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
        onConfirmKey(event);
        return event;
    })()`);

    assert.strictEqual(event.stopped, true);
    assert.strictEqual(event.prevented, true);
});
