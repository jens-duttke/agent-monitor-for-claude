'use strict';

/* The details' activity block as detail.js writes it, in the shared vm context
   the browser loads the scripts into (see global-scope.test.js). */

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
    vm.runInContext("state.labels.agent_running = 'Running';", context);

    return (code) => vm.runInContext(code, context);
}

const UNANSWERED = "[{ kind: 'tool', tool: 'Bash', input: { command: 'make' }, result: null, at: 1, result_at: null, is_error: false }]";

test('an unanswered call is flagged running only while its agent or session runs', () => {
    const run = controller();
    assert.ok(run(`activityMarkup(${UNANSWERED}, true)`).includes('>Running<'));
    // A finished agent or a stopped session: the call will never be answered.
    assert.ok(!run(`activityMarkup(${UNANSWERED}, false)`).includes('>Running<'));
});
