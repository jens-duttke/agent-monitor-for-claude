'use strict';

/* Which focused elements answer Enter themselves, as index.js decides it, in
   the shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('a focused disclosure or the splitter keeps Enter to itself', () => {
    // Enter on an activity step raised the session's window instead of
    // opening the step.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const controlTarget = vm.runInContext('controlTarget', context);
    // An element whose closest() answers for the one selector part it matches.
    const element = (matches) => ({ closest: (selector) => (selector.split(',').map((part) => part.trim()).includes(matches) ? {} : null) });

    assert.strictEqual(controlTarget(element('summary')), true);
    assert.strictEqual(controlTarget(element('[role="separator"]')), true);
    assert.strictEqual(controlTarget(element('button')), true);
    assert.strictEqual(controlTarget(element('div')), false);
});
