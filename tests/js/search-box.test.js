'use strict';

/* The search box's tooltip as search.js keeps it, in the shared vm context the
   browser loads the scripts into (see global-scope.test.js), on a stand-in input. */

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
    const input = { dataset: { tip: 'Ctrl K' }, setAttribute() {}, removeAttribute() {} };
    context.__input = input;
    context.document = { querySelector: () => null, getElementById: () => input };

    return { input, run: (code) => vm.runInContext(code, context) };
}

test('the shortcut hint comes back once the box holds a valid query again', () => {
    // It shares the attribute with the invalid-pattern message, which was
    // cleared by deleting it - the hint went with it for the session.
    const { input, run } = controller();
    run("state.searchError = true; updateSearchBox(); state.searchError = false; updateSearchBox();");
    assert.strictEqual(input.dataset.tip, 'Ctrl K');
    run('updateSearchBox();');
    assert.strictEqual(input.dataset.tip, 'Ctrl K');
});
