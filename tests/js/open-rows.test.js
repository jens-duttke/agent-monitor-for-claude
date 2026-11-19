'use strict';

/* Which collapsible rows of the details stay open across a rewrite, as
   detail.js remembers them, in the shared vm context the browser loads the
   scripts into (see global-scope.test.js). */

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

// A <details> as onStepToggle receives it: its id, its open state, and the owner above it.
function toggled(id, owner, open) {
    return `{ target: { open: ${open}, dataset: { openId: '${id}' }, matches: (selector) => selector === 'details[data-open-id]',
        closest: () => ({ dataset: { owner: '${owner}' } }) } }`;
}

const REPLIES = "[{ kind: 'text', text: 'one', at: 10 }, { kind: 'text', text: 'two', at: 20 }, { kind: 'text', text: 'three', at: 30 }]";

test('a reply row opened in one session stays closed in another', () => {
    const run = controller();
    run(`onStepToggle(${toggled('text-30-0', 'session:a#1', true)});`);
    assert.ok(run(`activityMarkup(${REPLIES}, false, 'session:a#1')`).includes('open'));
    assert.ok(!run(`activityMarkup(${REPLIES}, false, 'session:b#2')`).includes(' open'));
});

test('a session\'s open rows survive viewing one of its subagents', () => {
    const run = controller();
    run(`onStepToggle(${toggled('text-30-0', 'session:a#1', true)});`);
    run(`onStepToggle(${toggled('text-10-0', 'agent:a#1/x', true)});`);
    assert.ok(run(`activityMarkup(${REPLIES}, false, 'session:a#1')`).includes('open'));
});

test('an opened hook failure stays open when the session block is rewritten', () => {
    const run = controller();
    run("hookFailures.key = 's#1'; hookFailures.list = [{ at: 100, hook: 'PreToolUse', command: 'x', stderr: 'boom' }];");
    const session = "{ session_id: 's', pid: 1 }";
    run(`onStepToggle(${toggled('hook-failures', 'session:s#1', true)});`);
    run(`onStepToggle(${toggled('hook-100-PreToolUse', 'session:s#1', true)});`);
    const html = run(`hookFailuresMarkup(${session})`);
    assert.strictEqual((html.match(/ open/g) || []).length, 2, html);
});
