'use strict';

/* The sections for pins, Claude Desktop groups and Archived as the controller
   drives them, in the shared vm context the browser loads the scripts into:
   the Archived bucket, the History fetch it starts, the remembered folding of
   a group and the search scope. render and the bridge are recorders. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const stored = new Map();
    const localStorage = {
        getItem: (key) => (stored.has(key) ? stored.get(key) : null),
        setItem: (key, value) => { stored.set(key, String(value)); },
    };
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout: () => 0, clearTimeout, localStorage });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__renders = 0;
    context.__historyLoads = 0;
    vm.runInContext(`
        render = () => { __renders += 1; };
        runSearch = () => {};
        ensureHistoryLoaded = () => { __historyLoads += 1; };
        state.last = { sessions: [] };
    `, context);

    return { context, stored, run: (code) => vm.runInContext(code, context) };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('the archived bucket is off by default', () => {
    const { run } = controller();

    assert.equal(run('FILTER_KEYS.has("archived")'), true);
    assert.equal(run('DEFAULT_FILTER_KEYS.includes("archived")'), false);
    assert.equal(run('loadFilters().has("archived")'), false);
});

test('opening Archived fetches the History, which it shares with Older', () => {
    const { context, run } = controller();
    run('state.filters = new Set(["needs"]); state.history = null;');
    run('toggleSection("archived")');

    assert.equal(run('state.filters.has("archived")'), true);
    assert.equal(context.__historyLoads, 1);

    // With the cache in hand opening Older needs no second fetch.
    run('state.history = [];');
    run('toggleSection("older")');
    assert.equal(context.__historyLoads, 1);
});

test('a group folds as Desktop says until it is toggled here, and that sticks', () => {
    const { context, stored, run } = controller();
    run(`state.desktopGroups = logic.normalizeDesktopGroups({
        groups: [{ id: 'a', name: 'A', order: 0, collapsed: true }, { id: 'b', name: 'B', order: 1, collapsed: false }],
        members: {}, archived: [] });`);

    assert.deepEqual(plain([...run('currentGroupsView().collapsed')]).sort(), ['group:a']);

    run('toggleGroupSection("group:a"); toggleGroupSection("pinned");');
    assert.deepEqual(plain([...run('currentGroupsView().collapsed')]).sort(), ['pinned']);
    assert.ok(context.__renders >= 2);
    assert.deepEqual(JSON.parse(stored.get('amc-group-collapsed')), [['group:a', false], ['pinned', true]]);

    // A reload reads it back.
    run('state.groupCollapsed = new Map(); loadGroupCollapsed();');
    assert.deepEqual(plain([...run('currentGroupsView().collapsed')]).sort(), ['pinned']);
});

test('the remembered folding is capped and survives a broken value', () => {
    const { stored, run } = controller();
    run('for (let i = 0; i < 150; i += 1) { toggleGroupSection("group:g" + i); }');

    const kept = JSON.parse(stored.get('amc-group-collapsed'));
    assert.equal(kept.length, 100);
    assert.equal(kept[kept.length - 1][0], 'group:g149');

    stored.set('amc-group-collapsed', '{not json');
    run('loadGroupCollapsed();');
    assert.equal(run('state.groupCollapsed.size'), 0);
    stored.set('amc-group-collapsed', JSON.stringify([['pinned', true], [7, true], ['x', 'yes'], 'bare']));
    run('loadGroupCollapsed();');
    assert.deepEqual(plain([...run('state.groupCollapsed.entries()')]), [['pinned', true]]);
});

test('the search scope holds the sessions of a group whatever the chips', () => {
    const { run } = controller();
    run(`
        state.desktopGroups = logic.normalizeDesktopGroups({ groups: [{ id: 'g', name: 'G', order: 0, collapsed: true }],
            members: { m: 'g' }, archived: [] });
        state.filters = new Set();
        state.history = [];
        state.last = { sessions: [
            { session_id: 'm', cwd: 'D:\\\\p', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' },
            { session_id: 'o', cwd: 'D:\\\\p', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' },
        ] };
    `);

    assert.deepEqual(plain(run('currentSessionRefs()')).map((ref) => ref.session_id), ['m']);
});
