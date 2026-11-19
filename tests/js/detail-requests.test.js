'use strict';

/* What the details ask for when the selection moves while an answer is still
   out, as detail.js decides it, in the shared vm context the browser loads the
   scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

// The scripts, with every bridge call recorded and left to the test to answer.
function loadScripts() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__calls = [];
    vm.runInContext(`
        renderDetail = () => {};
        const answer = (name, id) => new Promise((resolve) => { __calls.push({ name, id, resolve }); });
        apiBridge = () => ({
            get_session_activity: (id) => answer('activity', id),
            get_agent_texts: (id, agentId) => answer('agent', agentId),
        });
    `, context);
    return context;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const session = (id) => ({ session_id: id, pid: 1, cwd: 'c', origin: 'local', exact_age_seconds: 5 });
const step = (text) => ({ steps: [{ kind: 'text', text, at: 1 }] });

test('a new selection asks for its own activity at once, and the old answer is dropped', async () => {
    // One call in flight held every other session's request back until it
    // answered, and the branch meant to drop a stale answer never ran.
    const context = loadScripts();
    context.__a = session('a');
    context.__b = session('b');
    vm.runInContext('requestSessionSteps(__a); requestSessionSteps(__b);', context);
    assert.deepStrictEqual(context.__calls.map((call) => call.id), ['a', 'b']);

    context.__calls[1].resolve(step('from b'));
    await settle();
    context.__calls[0].resolve(step('from a'));
    await settle();

    const steps = vm.runInContext('sessionSteps', context);
    assert.strictEqual(steps.key, vm.runInContext('rowKey(__b)', context));
    assert.deepStrictEqual([...steps.steps].map((entry) => entry.text), ['from b']);
});

test('a new agent selection asks for its own texts at once, and the old answer is dropped', async () => {
    const context = loadScripts();
    context.__s = session('s');
    context.__x = { id: 'ax', running: false, age_seconds: 1 };
    context.__y = { id: 'ay', running: false, age_seconds: 1 };
    vm.runInContext('requestAgentTexts(__s, __x); requestAgentTexts(__s, __y);', context);
    assert.deepStrictEqual(context.__calls.map((call) => call.id), ['ax', 'ay']);

    context.__calls[1].resolve({ task: 'task y', result: '', steps: [] });
    await settle();
    context.__calls[0].resolve({ task: 'task x', result: '', steps: [] });
    await settle();

    assert.strictEqual(vm.runInContext('agentTexts.task', context), 'task y');
});
