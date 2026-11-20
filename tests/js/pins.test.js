'use strict';

/* Pinning in the shared vm context the browser loads the scripts into: the
   toggle and its storage, the row menu, the delete flow and the periodic check. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');
const ID = '11111111-2222-4333-8444-555555555555';

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const storage = new Map();
    const localStorage = { getItem: (key) => (storage.has(key) ? storage.get(key) : null), setItem: (key, value) => storage.set(key, String(value)) };
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout: () => 0, clearTimeout, localStorage });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__calls = [];

    return { context, storage, run: (code) => vm.runInContext(code, context) };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('toggling a pin saves it, renders and asks for a new snapshot', () => {
    const { context, storage, run } = controller();
    run(`
        render = () => __calls.push('render');
        tick = () => __calls.push('tick');
        state.last = { sessions: [] };
        togglePin({ session_id: '${ID}', origin: 'Ubuntu' });
    `);
    assert.deepStrictEqual(JSON.parse(storage.get('amc-pins')), [{ id: ID, origin: 'Ubuntu' }]);
    assert.deepStrictEqual(plain(context.__calls), ['render', 'tick']);
    assert.deepStrictEqual(plain(run('keptSessionIds()')), [ID]);

    run(`togglePin({ session_id: '${ID}', origin: 'Ubuntu' })`);
    assert.deepStrictEqual(JSON.parse(storage.get('amc-pins')), []);
});

test('a session without a uuid cannot be pinned, and loadPins restores what was saved', () => {
    const { storage, run } = controller();
    run(`render = () => {}; tick = () => {}; togglePin({ session_id: 'abc' })`);
    assert.equal(run('state.pins.size'), 0);

    storage.set('amc-pins', JSON.stringify([{ id: ID, origin: 'local' }, { id: 'bad' }]));
    run('loadPins()');
    assert.deepStrictEqual(plain(run('Array.from(state.pins)')), [[ID, 'local']]);
    storage.set('amc-pins', '{{');
    run('loadPins()');
    assert.equal(run('state.pins.size'), 0);
});

test('kept ids list pins and group members once', () => {
    const { run } = controller();
    run(`
        state.pins = new Map([['${ID}', 'local']]);
        state.desktopGroups = { groups: [], members: new Map([['${ID}', 'g'], ['other', 'g']]), archived: new Set() };
    `);
    assert.deepStrictEqual(plain(run('keptSessionIds()')), [ID, 'other']);
});

test('the row menu offers pin for an unpinned session and unpin for a pinned one', async () => {
    for (const pinned of [false, true]) {
        const { context, run } = controller();
        run(`
            state.pins = new Map(${pinned ? `[['${ID}', 'local']]` : '[]'});
            state.sessionsByKey.set('k', { session_id: '${ID}', origin: 'local' });
            apiBridge = () => null;
            openMenu = (el, items) => { __calls.push(items.map((item) => item.key)); };
        `);
        await run(`openSessionMenu({ dataset: { key: 'k' }, isConnected: true }, null)`);
        const keys = plain(context.__calls[0]);
        assert.ok(keys.includes(pinned ? 'unpin' : 'pin'));
        assert.ok(!keys.includes(pinned ? 'pin' : 'unpin'));
    }
});

test('deleting a pinned history session through the confirm flow removes its pin', async () => {
    const { storage, run } = controller();
    run(`
        state.pins = new Map([['${ID}', 'local']]);
        state.history = [];
        render = () => {};
        toast = () => {};
        apiBridge = () => ({ delete_session: () => true });
        showConfirm = (title, body, detail, ok, onConfirm) => { __confirm = onConfirm; };
        confirmDeleteSession('${ID}', '/x', 'n', '1h', 'local');
    `);
    await run('__confirm()');
    assert.equal(run('state.pins.size'), 0);
    assert.deepStrictEqual(JSON.parse(storage.get('amc-pins')), []);
});

test('a failed delete keeps the pin', async () => {
    const { run } = controller();
    run(`
        state.pins = new Map([['${ID}', 'local']]);
        toast = () => {};
        apiBridge = () => ({ delete_session: () => false });
        showConfirm = (title, body, detail, ok, onConfirm) => { __confirm = onConfirm; };
        confirmDeleteSession('${ID}', '/x', 'n', '1h', 'local');
    `);
    await run('__confirm()');
    assert.equal(run('state.pins.size'), 1);
});

test('the pin check drops a pin only after two missing answers in a row, never on unknown', async () => {
    const { run } = controller();
    run(`
        render = () => {};
        tick = () => {};
        state.pins = new Map([['${ID}', 'local']]);
        __answer = 'missing';
        apiBridge = () => ({ check_pins: () => ({ '${ID}': __answer }) });
    `);
    await run('refreshPins()');
    assert.equal(run('state.pins.size'), 1);
    run(`__answer = 'unknown'`);
    await run('refreshPins()');
    run(`__answer = 'missing'`);
    await run('refreshPins()');
    assert.equal(run('state.pins.size'), 1);
    await run('refreshPins()');
    assert.equal(run('state.pins.size'), 0);
});
