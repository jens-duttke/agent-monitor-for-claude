'use strict';

/* What the app keeps selected when the selected session or agent goes away,
   in the shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

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

    vm.runInContext('markSelectedItem = () => {};', context);
    context.document = { body: { classList: { remove: () => {}, add: () => {}, toggle: () => {} } } };

    return (code) => vm.runInContext(code, context);
}

test('a tool filter does not carry over to the session the selection moves to', () => {
    // The tool belonged to a session that left the list; the next one was
    // never filtered by the user.
    const run = controller();
    run("state.selectedKey = 'gone#1'; state.selectedTool = 'Bash'; state.scrollToActivity = true; state.listKeys = ['other#2']; ensureSelection();");
    assert.strictEqual(run('state.selectedKey'), 'other#2');
    assert.strictEqual(run('state.selectedTool'), null);
    assert.strictEqual(run('state.scrollToActivity'), false);
});

test('a subagent tool filter goes with the subagent', () => {
    // Outside the graph the snapshot carries no agents, and the pane falls back
    // to the session - which is not to be filtered by the agent's tool.
    const run = controller();
    run("state.selectedAgent = 'a1'; state.selectedTool = 'Grep'; state.scrollToActivity = true;");
    assert.strictEqual(run("selectedAgentOf({ agents: [] })"), null);
    assert.strictEqual(run('state.selectedTool'), null);
    assert.strictEqual(run('state.scrollToActivity'), false);
});
