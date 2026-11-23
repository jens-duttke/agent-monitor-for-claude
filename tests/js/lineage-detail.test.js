'use strict';

/* The details' "Started from" / "Forked from" lines and the sessions started
   from a session, and where a click on one of them leads - in the shared vm
   context the browser loads the scripts into. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');
const PARENT = '11111111-2222-4333-8444-555555555555';
const CHILD = '22222222-3333-4444-8555-666666666666';
const FORK = '33333333-4444-4555-8666-777777777777';

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
    const run = (code) => vm.runInContext(code, context);
    run(`
        state.labels = Object.assign({}, DEFAULT_LABELS);
        state.desktopGroups = logic.normalizeDesktopGroups({ groups: [], members: {}, archived: [], lineage: [
            { session: '${CHILD}', parent: '${PARENT}', kind: 'started' },
            { session: '${FORK}', parent: '${PARENT}', kind: 'forked' },
        ] });
        render = () => __calls.push('render');
        renderDetail = () => __calls.push('detail');
        markSelectedItem = () => {};
        refreshGraphFocus = () => {};
        document = { body: { classList: { add: () => {}, remove: () => {}, toggle: () => {} } } };
    `);
    return { context, run };
}

const built = (id, name, extra) => Object.assign({ session_id: id, name: name, hook_errors: [], model_history: [], cli_history: [] }, extra || {});

test('a child names the session it was started from, and the parent lists what it started', () => {
    const { run } = controller();
    run(`state.sessionsByKey = new Map([['p', ${JSON.stringify(built(PARENT, 'Parent <one>'))}], ['c', ${JSON.stringify(built(CHILD, 'Child'))}]]);`);

    const child = run(`sessionBodyMarkup(state.sessionsByKey.get('c'))`);
    assert.match(child, /Started from<\/span>/);
    assert.match(child, /data-action="lineage" data-session="11111111-2222-4333-8444-555555555555">Parent &lt;one&gt;<\/button>/);

    const parent = run(`sessionBodyMarkup(state.sessionsByKey.get('p'))`);
    assert.match(parent, /Started from here<\/span>/);
    assert.match(parent, /data-session="22222222-3333-4444-8555-666666666666">Child<\/button>/);
    // A fork the app does not know: its id as text, marked as a fork.
    assert.match(parent, /<span class="mono">33333333<\/span> · fork/);
});

test('a fork reads "Forked from", and an unknown parent is plain text', () => {
    const { run } = controller();
    run(`state.sessionsByKey = new Map([['f', ${JSON.stringify(built(FORK, 'Fork'))}]]);`);
    const markup = run(`sessionBodyMarkup(state.sessionsByKey.get('f'))`);
    assert.match(markup, /Forked from<\/span><span class="kv-value"><span class="mono">11111111<\/span>/);
    assert.doesNotMatch(markup, /data-action="lineage"/);
});

test('a parent among the fetched past sessions is named by its own title', () => {
    const { run } = controller();
    run(`state.sessionsByKey = new Map([['c', ${JSON.stringify(built(CHILD, 'Child'))}]]);
        state.history = [{ session_id: '${PARENT}', title: 'Past parent' }];`);
    assert.match(run(`sessionBodyMarkup(state.sessionsByKey.get('c'))`), />Past parent<\/button>/);
});

test('a click selects a listed session at once', () => {
    const { run } = controller();
    run(`state.sessionsByKey = new Map([['p', ${JSON.stringify(built(PARENT, 'Parent'))}], ['c', ${JSON.stringify(built(CHILD, 'Child'))}]]);
        state.listKeys = ['p', 'c']; state.selectedKey = 'c';
        openLineageSession('${PARENT}');`);
    assert.strictEqual(run('state.selectedKey'), 'p');
    assert.strictEqual(run('state.pendingSelect'), null);
});

test('a click on a session not listed opens its section and selects it once listed', () => {
    const { run } = controller();
    run(`state.sessionsByKey = new Map([['c', ${JSON.stringify(built(CHILD, 'Child'))}]]);
        state.listKeys = ['c']; state.selectedKey = 'c';
        state.filters = new Set(['needs']);
        state.history = [{ session_id: '${PARENT}', title: 'Parent' }]; state.historyStale = false;
        state.desktopGroups.archived = new Set(['${PARENT}']);
        state.last = { sessions: [] };
        openLineageSession('${PARENT}');`);
    assert.strictEqual(run(`state.filters.has('archived')`), true);
    assert.strictEqual(run('state.pendingSelect'), PARENT);

    // The next render lists it: it is selected; a render that does not ends the wish.
    run(`state.sessionsByKey.set('p', ${JSON.stringify(built(PARENT, 'Parent'))}); state.listKeys = ['c', 'p']; applyPendingSelect();`);
    assert.strictEqual(run('state.selectedKey'), 'p');
    run(`state.pendingSelect = 'gone'; applyPendingSelect();`);
    assert.strictEqual(run('state.pendingSelect'), null);
    assert.strictEqual(run('state.selectedKey'), 'p');
});

test('a click unfolds the Desktop group and sub-heading that hide the session', () => {
    const { run } = controller();
    run(`state.desktopGroups.groups = [{ id: 'g', name: 'G', order: 0, collapsed: true }];
        state.desktopGroups.members = new Map([['${PARENT}', 'g']]);
        state.groupSubCollapsed = new Set(['group:g|recent']);
        state.group = 'urgency';
        state.filters = new Set(['idle']);
        state.sessionsByKey = new Map([['p', ${JSON.stringify(built(PARENT, 'Parent', { status: 'awaiting_input', alive: true }))}]]);
        state.listKeys = []; state.last = { sessions: [] };
        openLineageSession('${PARENT}');`);
    assert.strictEqual(run(`state.groupCollapsed.get('group:g')`), false);
    assert.strictEqual(run(`state.groupSubCollapsed.has('group:g|recent')`), false);
    assert.strictEqual(run('state.pendingSelect'), PARENT);
});

test('in the Project grouping a click opens the section and the project', () => {
    const { run } = controller();
    run(`state.group = 'project';
        state.collapsed = new Set(['proj']);
        state.filters = new Set(['needs']);
        state.sessionsByKey = new Map([['p', ${JSON.stringify(built(PARENT, 'Parent', { status: 'awaiting_input', alive: true, project_key: 'proj' }))}]]);
        state.listKeys = []; state.last = { sessions: [] };
        openLineageSession('${PARENT}');`);
    assert.strictEqual(run(`state.collapsed.has('proj')`), false);
    assert.strictEqual(run(`logic.sectionActive('recent', state.filters)`), true);
});

test('a past session the fetch brought into a folded project is unfolded once, then selected', () => {
    const { run } = controller();
    run(`globalThis.__timers = []; setTimeout = (fn) => { __timers.push(fn); return 0; };
        state.group = 'project'; state.collapsed = new Set(['proj']);
        state.pendingSelect = '${PARENT}'; state.pendingRevealed = false; state.historyLoading = false;
        state.sessionsByKey = new Map([['p', ${JSON.stringify(built(PARENT, 'Parent', { status: 'completed', is_history: true, project_key: 'proj' }))}]]);
        state.listKeys = [];
        applyPendingSelect();`);
    assert.strictEqual(run(`state.collapsed.has('proj')`), false);
    assert.strictEqual(run('state.pendingSelect'), PARENT);
    assert.strictEqual(run('__timers.length'), 1);
    // Still not listed on the next render (a search hides it): the wish ends, nothing loops.
    run('applyPendingSelect()');
    assert.strictEqual(run('state.pendingSelect'), null);
});

test('a session no click can bring on screen is plain text, never a dead link', () => {
    const { run } = controller();
    const sessions = `new Map([['p', ${JSON.stringify(built(PARENT, 'Parent'))}], ['c', ${JSON.stringify(built(CHILD, 'Child'))}]])`;
    run(`state.sessionsByKey = ${sessions}; state.listKeys = ['c']; state.group = 'graph';`);
    const graph = run(`sessionBodyMarkup(state.sessionsByKey.get('c'))`);
    assert.match(graph, /<span class="kv-value">Parent<\/span>/);
    assert.doesNotMatch(graph, /data-action="lineage"/);
    run(`openLineageSession('${PARENT}')`);
    assert.strictEqual(run('state.pendingSelect'), null);

    run(`state.group = 'urgency'; state.search = 'something';`);
    assert.doesNotMatch(run(`sessionBodyMarkup(state.sessionsByKey.get('c'))`), /data-action="lineage"/);
    run(`state.search = '';`);
    assert.match(run(`sessionBodyMarkup(state.sessionsByKey.get('c'))`), /data-action="lineage"/);
});

test('the wish waits while the past sessions are still loading', () => {
    const { run } = controller();
    run(`state.pendingSelect = '${PARENT}'; state.historyLoading = true; state.listKeys = []; applyPendingSelect();`);
    assert.strictEqual(run('state.pendingSelect'), PARENT);
});
