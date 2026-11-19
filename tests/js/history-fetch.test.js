'use strict';

/* The History fetch as the controller drives it: when the cache is fetched
   again, and what the list holds meanwhile. The controller scripts run in one
   shared vm context, the way the browser loads them (see global-scope.test.js);
   render, the bridge and the search are replaced by recorders, so no DOM is
   needed. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function loadController() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const window = { addEventListener: () => {} };
    const context = vm.createContext({ window, setTimeout, clearTimeout, localStorage: { setItem: () => {}, getItem: () => null } });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }

    return context;
}

// A controller whose bridge answers each get_history call with a promise the test settles.
function harness() {
    const context = loadController();
    const calls = [];
    context.__calls = calls;
    context.__renders = [];
    vm.runInContext(`
        render = (snapshot) => { __renders.push(state.history); };
        runSearch = () => {};
        apiBridge = () => ({
            get_history: (seconds) => new Promise((resolve) => { __calls.push({ seconds, resolve }); }),
        });
        state.last = { sessions: [] };
    `, context);

    return { context, calls, run: (code) => vm.runInContext(code, context) };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('widening the window while a fetch runs fetches the wider window once it lands', async () => {
    const { calls, run } = harness();
    run("state.historyRange = '24h'; ensureHistoryLoaded();");
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].seconds, 86400);

    run("setHistoryRange('7d');");
    await settle();
    calls[0].resolve([{ session_id: 'past-a', age_seconds: 100 }]);
    await settle();

    assert.strictEqual(calls.length, 2);
    assert.strictEqual(calls[1].seconds, 604800);
    calls[1].resolve([{ session_id: 'past-a', age_seconds: 100 }, { session_id: 'past-b', age_seconds: 300000 }]);
    await settle();
    assert.strictEqual(run('state.historyLoadedSeconds'), 604800);
});

test('a session that leaves the live list while a fetch runs is fetched again', async () => {
    const { calls, run } = harness();
    run("state.last = { sessions: [{ session_id: 'live-a' }, { session_id: 'live-b' }] }; ensureHistoryLoaded();");
    // live-b ends and leaves the snapshot while the scan is still reading.
    run("state.last = { sessions: [{ session_id: 'live-a' }] };");
    calls[0].resolve([]);
    await settle();

    assert.strictEqual(calls.length, 2);
});

test('a refresh keeps the cached rows on screen until the new ones land', async () => {
    // Blanking the cache first made the render drop every past session for a
    // moment, and with it a selected one - the selection jumped to the first row.
    const { calls, run, context } = harness();
    run("ensureHistoryLoaded();");
    calls[0].resolve([{ session_id: 'past-a', age_seconds: 100 }]);
    await settle();

    run('refreshHistory();');
    await settle();
    assert.strictEqual(calls.length, 2);
    assert.ok(context.__renders.every((rows) => rows === null || rows.length === 1) && run('state.history.length') === 1);
    calls[1].resolve([{ session_id: 'past-a', age_seconds: 100 }, { session_id: 'past-b', age_seconds: 200 }]);
    await settle();
    assert.strictEqual(run('state.history.length'), 2);
});

test('opening Older after its cache went stale fetches it again', async () => {
    const { calls, run } = harness();
    run("state.filters.delete('history'); ensureHistoryLoaded();");
    calls[0].resolve([]);
    await settle();

    run("state.historyStale = true; toggleSection('older');");
    await settle();
    assert.strictEqual(calls.length, 2);
});
