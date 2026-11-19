'use strict';

/* Applying the bootstrap and retrying it, as index.js does, in the shared vm
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
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {} });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.document = { getElementById: () => null, title: '' };
    context.__bootstraps = 0;
    context.__polls = 0;
    vm.runInContext(`
        refreshSearchOptButtons = refreshCompactButton = refreshSettingsButton = refreshGroupSwitch = () => {};
        apiBridge = () => ({});
        callBootstrap = () => { __bootstraps += 1; return Promise.resolve({ labels: {}, poll_interval: 2 }); };
        callSnapshot = () => Promise.resolve({ sessions: [] });
        render = () => {};
        const realStartPoll = startPoll;
        startPoll = () => { __polls += 1; realStartPoll(); };
    `, context);

    return { context, run: (code) => vm.runInContext(code, context) };
}

test('a bootstrap the bridge answered is not asked for again, even with no labels', async () => {
    // Retried on every tick, it cancelled whatever search was running.
    const { context, run } = controller();
    run("applyBootstrap({ labels: {}, poll_interval: 5 });");
    await run('tick()');
    await run('tick()');
    assert.strictEqual(context.__bootstraps, 0);
});

test('an interval recovered by a retried bootstrap restarts the running poll', async () => {
    const { context, run } = controller();
    run("state.bootstrapLoaded = false; apiBridge = () => null; applyBootstrap({ labels: {}, poll_interval: 5 }); apiBridge = () => ({}); startPoll();");
    const polls = context.__polls;
    await run('tick()');
    assert.strictEqual(run('state.pollInterval'), 2);
    assert.strictEqual(context.__polls, polls + 1);
});
