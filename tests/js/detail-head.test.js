'use strict';

/* The session details' head as detail.js keeps it, in the shared vm context
   the browser loads the scripts into (see global-scope.test.js), on a small
   stand-in for the DOM that records which elements get rewritten. */

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

// An element whose innerHTML writes are counted, and which hands out one child
// per class name once its markup has been written.
function element() {
    const children = new Map();
    const node = { writes: 0, children, parentNode: null };
    let html = '';
    Object.defineProperty(node, 'innerHTML', {
        get: () => html,
        set: (value) => {
            html = value;
            node.writes += 1;
            children.clear();
            for (const match of value.matchAll(/class="([\w-]+)"/g)) {
                const child = element();
                child.parentNode = node;
                children.set(match[1], child);
            }
        },
    });
    node.querySelector = (selector) => children.get(selector.replace(/^\./, '')) || null;

    return node;
}

test('a status line that ticks rewrites the status, not the actions beside it', () => {
    // Rewriting the whole head dropped a selection in it and the focus of a
    // button - Enter then raised the session's window instead.
    const run = controller();
    const head = element();
    run('globalThis.__head = undefined;');
    const session = (tip) => JSON.stringify({ name: 'S', status: 'working', status_label: 'Working', status_tip: tip, age_seconds: 5, session_id: 's', cwd: 'c' });
    const vmContext = run('globalThis');
    vmContext.__head = head;

    run(`updateDetailHead(__head, ${session('Working\\nfor 1m')});`);
    const actions = head.querySelector('.detail-actions');
    const writesBefore = actions.writes;
    run(`updateDetailHead(__head, ${session('Working\\nfor 2m')});`);

    assert.strictEqual(head.querySelector('.detail-actions'), actions);
    assert.strictEqual(actions.writes, writesBefore);
    assert.strictEqual(head.writes, 1);
});
