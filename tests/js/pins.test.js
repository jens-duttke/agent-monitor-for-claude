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

test('pinning past the limit says so instead of doing nothing', () => {
    const { context, run } = controller();
    run(`
        render = () => {};
        tick = () => {};
        toast = (message) => __calls.push(message);
        state.labels = Object.assign({}, DEFAULT_LABELS);
        state.pins = new Map();
        for (let index = 0; index < logic.MAX_PINS; index += 1) {
            state.pins.set('00000000-0000-4000-8000-' + String(index).padStart(12, '0'), 'local');
        }
        togglePin({ session_id: '${ID}', origin: 'local' });
    `);
    assert.equal(run('state.pins.size'), run('logic.MAX_PINS'));
    assert.equal(run(`state.pins.has('${ID}')`), false);
    assert.deepStrictEqual(plain(context.__calls), [run('logic.fmt(DEFAULT_LABELS.pins_full, { max: logic.MAX_PINS })')]);
    assert.ok(!plain(context.__calls)[0].includes('{max}'));
});

const KEPT_RAW = { session_id: ID, pid: 0, cwd: '/x', alive: false, kept: true, has_transcript: true, has_activity: true,
    last_entry_kind: 'assistant', last_stop_reason: 'end_turn', age_seconds: 60, origin: 'local' };

test('a kept session carries its kept flag through buildSession', () => {
    const { run } = controller();
    run(`state.labels = Object.assign({}, DEFAULT_LABELS)`);
    assert.equal(run(`logic.buildSession(${JSON.stringify(KEPT_RAW)}, state.labels).kept`), true);
    assert.equal(run(`logic.buildSession(${JSON.stringify(Object.assign({}, KEPT_RAW, { kept: undefined }))}, state.labels).kept`), false);
});

test('an ended kept session offers delete and no window; a live one the reverse', async () => {
    for (const alive of [false, true]) {
        const { context, run } = controller();
        run(`
            state.labels = Object.assign({}, DEFAULT_LABELS);
            state.sessionsByKey.set('k', { session_id: '${ID}', origin: 'local', cwd: '/x', kept: true, alive: ${alive} });
            apiBridge = () => null;
            openMenu = (el, items) => { __calls.push(items.map((item) => item.key)); };
        `);
        await run(`openSessionMenu({ dataset: { key: 'k' }, isConnected: true }, null)`);
        assert.equal(plain(context.__calls[0]).includes('delete'), !alive);
        assert.equal(run(`hasWindow(state.sessionsByKey.get('k'))`), alive);
    }
});

test('deleting an ended kept session quotes the age the snapshot row shows', () => {
    const { run } = controller();
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        globalThis.__quoted = null;
        confirmDeleteSession = (id, cwd, name, age) => { __quoted = age; };
        state.historyReceivedAt = null;
        state.receivedAt = Date.now() - 3600 * 1000;
        runSessionAction('delete', { session_id: '${ID}', pid: 0, cwd: '/x', name: 'n', kept: true, alive: false, age_seconds: 60, origin: 'local' });
    `);
    assert.strictEqual(run('__quoted'), run('logic.formatAge(3660, state.labels)'));
});

test('a live kept session cannot be deleted from the action', () => {
    const { run } = controller();
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        globalThis.__quoted = null;
        confirmDeleteSession = () => { __quoted = 'asked'; };
        state.receivedAt = Date.now();
        runSessionAction('delete', { session_id: '${ID}', pid: 5, cwd: '/x', name: 'n', kept: true, alive: true, age_seconds: 60, origin: 'local' });
    `);
    assert.strictEqual(run('__quoted'), null);
});

test('the search scope is the visible set in every grouping', () => {
    for (const [group, expected] of [['urgency', [ID]], ['project', []], ['graph', []]]) {
        const { run } = controller();
        run(`
            state.compact = false;
            state.group = '${group}';
            state.filters = new Set(['needs']);
            state.pins = new Map([['${ID}', 'local']]);
            state.last = { sessions: [${JSON.stringify(KEPT_RAW)}] };
            state.history = [];
        `);
        assert.deepStrictEqual(plain(run('collectSearchRefs(true)')).map((ref) => ref.session_id), expected, group);
    }
});

const DESKTOP = (starred) => `state.desktopGroups = { groups: [], members: new Map(), archived: new Set(), starred: new Set(${JSON.stringify(starred)}) };`;

test('kept ids add the sessions pinned in Claude Desktop once', () => {
    const { run } = controller();
    run(`state.pins = new Map([['${ID}', 'local']]); ${DESKTOP([ID, 'other'])}`);
    assert.deepStrictEqual(plain(run('keptSessionIds()')), [ID, 'other']);
});

test('a session pinned in Desktop lists under Pinned', () => {
    const { run } = controller();
    run(DESKTOP([ID]));
    assert.equal(run(`currentGroupsView().pinned.has('${ID}')`), true);
});

test('a session pinned only in Desktop gets a note in the row menu, not unpin', async () => {
    const { context, run } = controller();
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        ${DESKTOP([ID])}
        state.sessionsByKey.set('k', { session_id: '${ID}', origin: 'local' });
        apiBridge = () => null;
        openMenu = (el, items) => { __calls.push(items); };
    `);
    await run(`openSessionMenu({ dataset: { key: 'k' }, isConnected: true }, null)`);
    const items = plain(context.__calls[0]);
    assert.ok(!items.some((item) => item.key === 'pin' || item.key === 'unpin'));
    assert.ok(items.some((item) => item.note === true && item.label === run('DEFAULT_LABELS.pinned_in_desktop')));
});

test('the detail head shows the Desktop pin as a note, not a button', () => {
    const { run } = controller();
    run(`state.labels = Object.assign({}, DEFAULT_LABELS); ${DESKTOP([ID])}`);
    const html = run(`actionsMarkup({ session_id: '${ID}', origin: 'local', alive: false })`);
    assert.ok(!html.includes('data-action="pin"') && !html.includes('data-action="unpin"'));
    assert.ok(html.includes(run('DEFAULT_LABELS.pinned_in_desktop')));
});

test('P on a session pinned only in Desktop changes nothing and says why', () => {
    const { context, storage, run } = controller();
    run(`
        render = () => __calls.push('render');
        tick = () => {};
        toast = (message) => __calls.push(message);
        state.labels = Object.assign({}, DEFAULT_LABELS);
        ${DESKTOP([ID])}
        togglePin({ session_id: '${ID}', origin: 'local' });
    `);
    assert.equal(run('state.pins.size'), 0);
    assert.equal(storage.has('amc-pins'), false);
    assert.deepStrictEqual(plain(context.__calls), [run('DEFAULT_LABELS.pinned_in_desktop')]);
});

test('a session pinned in both offers no unpin, only the Desktop note', async () => {
    const { context, run } = controller();
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        state.pins = new Map([['${ID}', 'local']]);
        ${DESKTOP([ID])}
        state.sessionsByKey.set('k', { session_id: '${ID}', origin: 'local' });
        apiBridge = () => null;
        openMenu = (el, items) => { __calls.push(items); };
    `);
    await run(`openSessionMenu({ dataset: { key: 'k' }, isConnected: true }, null)`);
    const items = plain(context.__calls[0]);
    assert.ok(!items.some((item) => item.key === 'pin' || item.key === 'unpin'));
    assert.ok(items.some((item) => item.note === true && item.label === run('DEFAULT_LABELS.pinned_in_desktop')));
    assert.ok(!run(`actionsMarkup({ session_id: '${ID}', origin: 'local', alive: false })`).includes('data-action="unpin"'));
});

const OTHER = '22222222-2222-4333-8444-555555555555';

test('a Desktop pin replaces an own pin, saved and rendered once', async () => {
    const { context, storage, run } = controller();
    run(`
        render = () => __calls.push('render');
        state.last = { sessions: [] };
        state.pins = new Map([['${ID}', 'local'], ['${OTHER}', 'local']]);
        apiBridge = () => ({ get_desktop_groups: () => ({ groups: [], members: {}, archived: [], starred: ['${ID}'] }) });
    `);
    await run('refreshDesktopGroups()');
    assert.deepStrictEqual(plain(run('Array.from(state.pins.keys())')), [OTHER]);
    assert.deepStrictEqual(JSON.parse(storage.get('amc-pins')), [{ id: OTHER, origin: 'local' }]);
    assert.deepStrictEqual(plain(context.__calls), ['render']);
    assert.equal(run(`currentGroupsView().pinned.has('${ID}')`), true);
    // The same answer again changes nothing.
    await run('refreshDesktopGroups()');
    assert.deepStrictEqual(plain(context.__calls), ['render']);
});

test('an own pin added for an id Desktop already pins lapses on the next answer', async () => {
    const { storage, run } = controller();
    run(`
        render = () => {};
        ${DESKTOP([ID])}
        state.pins = new Map([['${ID}', 'local']]);
        apiBridge = () => ({ get_desktop_groups: () => ({ groups: [], members: {}, archived: [], starred: ['${ID}'] }) });
    `);
    await run('refreshDesktopGroups()');
    assert.equal(run('state.pins.size'), 0);
    assert.deepStrictEqual(JSON.parse(storage.get('amc-pins')), []);
});

test('no Desktop answer, or one without the id, never drops an own pin', async () => {
    for (const answer of ['null', '({ starred: [] })', '({ starred: "x" })', `({ starred: ['${OTHER}'] })`]) {
        const { storage, run } = controller();
        run(`
            render = () => {};
            state.pins = new Map([['${ID}', 'local']]);
            apiBridge = () => ({ get_desktop_groups: () => ${answer} });
        `);
        await run('refreshDesktopGroups()');
        assert.equal(run('state.pins.size'), 1, answer);
        assert.equal(storage.has('amc-pins'), false, answer);
    }
    const { run } = controller();
    run(`
        state.pins = new Map([['${ID}', 'local']]);
        apiBridge = () => ({ get_desktop_groups: () => { throw new Error('down'); } });
    `);
    await run('refreshDesktopGroups().catch(() => {})');
    assert.equal(run('state.pins.size'), 1);
});

test('the pin check never asks about the sessions pinned in Desktop', async () => {
    const { context, run } = controller();
    run(`
        ${DESKTOP([ID])}
        apiBridge = () => ({ check_pins: (refs) => { __calls.push(refs); return {}; } });
    `);
    await run('refreshPins()');
    assert.deepStrictEqual(plain(context.__calls), []);
});

test('an ended kept Desktop chat pinned here says so under Pinned', () => {
    const { run } = controller();
    run(`state.labels = Object.assign({}, DEFAULT_LABELS); state.pins = new Map([['${ID}', 'local']]); ${DESKTOP([])}`);
    const raw = JSON.stringify(Object.assign({}, KEPT_RAW, { entrypoint: 'claude-desktop' }));
    const markup = run(`logic.pinSourceMarkup(logic.pinSource(logic.buildSession(${raw}, state.labels), state.pins,
        state.desktopGroups.starred, state.desktopGroups.members), state.labels)`);
    assert.ok(markup.includes(run('DEFAULT_LABELS.pinned_in_agent_monitor')));
    // Without the transcript's entrypoint it is not known as a Desktop chat.
    assert.equal(run(`logic.pinSource(logic.buildSession(${JSON.stringify(KEPT_RAW)}, state.labels), state.pins,
        state.desktopGroups.starred, state.desktopGroups.members)`), null);
});
