'use strict';

// Which session Claude Desktop started or forked a session from: the bridge
// answer's normalization, the details' lookup, and the graph's edges, dimmed
// nodes and stacking order.

const test = require('node:test');
const assert = require('node:assert');
const logic = require('../../agent_monitor_for_claude/ui/logic.js');

const lineage = (pairs) => logic.normalizeLineage(pairs.map(([session, parent, kind]) => ({ session: session, parent: parent, kind: kind || 'started' })));

const session = (id, extra) => Object.assign({ session_id: id, name: 'Session ' + id, status: 'awaiting_input', age_seconds: 10, agents: [], messages: [] }, extra || {});
const entry = (id, extra, dimmed) => ({ key: 'k-' + id, session: session(id, extra), dimmed: Boolean(dimmed) });
const lineageEdges = (model) => model.edges.filter((edge) => edge.kind === 'lineage').map((edge) => edge.from + '>' + edge.to + ':' + edge.relation);

test('lineage: the bridge list becomes a child -> parent map, malformed entries dropped', () => {
    const result = logic.normalizeLineage([
        { session: 'b', parent: 'a', kind: 'started' },
        { session: 'c', parent: 'a', kind: 'forked' },
        { session: 'b', parent: 'z', kind: 'started' },
        { session: 'd', parent: 'd', kind: 'started' },
        { session: 'e', parent: 'a', kind: 'constructor' },
        { session: 'f', parent: 5, kind: 'started' },
        { session: '', parent: 'a', kind: 'started' },
        null, 'x', 7,
    ]);
    assert.deepStrictEqual(Array.from(result), [['b', { parent: 'a', kind: 'started' }], ['c', { parent: 'a', kind: 'forked' }]]);
    assert.strictEqual(logic.normalizeLineage('x').size, 0);
    assert.strictEqual(logic.normalizeLineage(undefined).size, 0);
});

test('lineage: normalizeDesktopGroups carries it and the signature changes with it', () => {
    const before = logic.normalizeDesktopGroups({ groups: [], members: {}, archived: [], lineage: [] });
    const after = logic.normalizeDesktopGroups({ groups: [], members: {}, archived: [], lineage: [{ session: 'b', parent: 'a', kind: 'forked' }] });
    assert.strictEqual(after.lineage.get('b').kind, 'forked');
    assert.notStrictEqual(logic.desktopGroupsSignature(before), logic.desktopGroupsSignature(after));
});

test('lineage: a session knows its parent and the sessions started from it, never itself', () => {
    const links = lineage([['b', 'a'], ['c', 'a', 'forked'], ['a', 'root']]);
    assert.deepStrictEqual(logic.lineageOf(links, 'a'), { parent: { id: 'root', kind: 'started' }, children: [{ id: 'b', kind: 'started' }, { id: 'c', kind: 'forked' }] });
    assert.deepStrictEqual(logic.lineageOf(links, 'b'), { parent: { id: 'a', kind: 'started' }, children: [] });
    assert.deepStrictEqual(logic.lineageOf(links, 'x'), { parent: null, children: [] });
    assert.deepStrictEqual(logic.lineageOf(null, 'a'), { parent: null, children: [] });
    // A raw self-reference that bypassed normalizeLineage still yields nothing.
    assert.deepStrictEqual(logic.lineageOf(new Map([['s', { parent: 's', kind: 'started' }]]), 's'), { parent: null, children: [] });
});

test('lineage: the graph dims a direct parent or child outside the window, nothing further', () => {
    const links = lineage([['b', 'a'], ['c', 'b'], ['x', 'y']]);
    const others = [session('a'), session('c'), session('y'), session('a')];
    const dimmed = logic.graphLineageNeighbors(new Set(['b']), others, links);
    assert.deepStrictEqual(dimmed.map((item) => item.session_id), ['a', 'c']);
    assert.deepStrictEqual(logic.graphLineageNeighbors(new Set(['a', 'b']), [session('c')], links).map((item) => item.session_id), ['c']);
    assert.deepStrictEqual(logic.graphLineageNeighbors(new Set(['b']), others, new Map()), []);
    assert.deepStrictEqual(logic.graphLineageNeighbors(new Set(['b']), others, null), []);
});

test('lineage graph: a started and a forked edge run from parent to child', () => {
    const model = logic.graphModel([entry('a'), entry('b'), entry('c')], null, null, null, lineage([['b', 'a'], ['c', 'a', 'forked']]));
    assert.deepStrictEqual(lineageEdges(model), ['s:k-a>s:k-b:started', 's:k-a>s:k-c:forked']);
    const edge = model.edges.find((item) => item.kind === 'lineage');
    assert.deepStrictEqual(edge.heads, [{ node: 's:k-b', mark: 'lineage', slot: 'tip' }]);
});

test('lineage graph: a chain draws one edge per link, and a self-reference none', () => {
    const raw = new Map([['b', { parent: 'a', kind: 'started' }], ['c', { parent: 'b', kind: 'started' }], ['a', { parent: 'a', kind: 'started' }]]);
    const model = logic.graphModel([entry('c'), entry('b'), entry('a')], null, null, null, raw);
    assert.deepStrictEqual(lineageEdges(model), ['s:k-a>s:k-b:started', 's:k-b>s:k-c:started']);
});

test('lineage graph: an edge to a session that is not drawn is left out', () => {
    const model = logic.graphModel([entry('b')], null, null, null, lineage([['b', 'a']]));
    assert.deepStrictEqual(lineageEdges(model), []);
});

test('lineage graph: a parent outside the window is drawn dimmed, with its box alone', () => {
    const parent = entry('a', { agents: [{ id: 'a0123456789abcdef', running: true, age_seconds: 1 }], processes: 2, messages: [{ peer: 'b', direction: 'sent', age_seconds: 1 }] }, true);
    const model = logic.graphModel([entry('b'), parent], 3600, null, null, lineage([['b', 'a']]));
    const node = model.nodes.find((item) => item.id === 's:k-a');
    assert.strictEqual(node.dimmed, true);
    assert.strictEqual(node.below, 0);
    assert.strictEqual(model.nodes.filter((item) => item.root === 's:k-a' && item.id !== 's:k-a').length, 0);
    const edge = model.edges.find((item) => item.kind === 'lineage');
    assert.strictEqual(edge.dimmed, true);
    const markup = logic.graphMarkup(logic.graphLayout(model), { graph_outside_window: 'Outside the time window' }, null);
    assert.match(markup, /class="gnode gnode-session status-awaiting_input dimmed"/);
    assert.match(markup, /Outside the time window/);
    assert.match(markup, /class="edge edge-lineage dimmed"/);
});

test('lineage graph: the edge stays when either side is folded, apart from their messages', () => {
    const links = lineage([['b', 'a', 'forked']]);
    const a = entry('a', { agents: [{ id: 'a0123456789abcdef', running: true, age_seconds: 1 }], messages: [{ peer: 'Session b', direction: 'sent', age_seconds: 5 }] });
    const model = logic.graphModel([a, entry('b')], null, null, new Set(['a', 'b']), links);
    assert.strictEqual(model.nodes.find((node) => node.id === 's:k-a').collapsed, true);
    assert.deepStrictEqual(lineageEdges(model), ['s:k-a>s:k-b:forked']);
    assert.strictEqual(model.edges.filter((edge) => edge.kind === 'message').length, 1);
    const layout = logic.graphLayout(model);
    const paths = layout.edges.filter((edge) => edge.kind === 'lineage' || edge.kind === 'message').map((edge) => edge.path);
    assert.strictEqual(paths.length, 2);
    assert.notStrictEqual(paths[0], paths[1]);
});

test('lineage graph: a parent is stacked above its children, also across a chain', () => {
    const links = lineage([['c', 'b'], ['b', 'a'], ['d', 'a']]);
    const model = logic.graphModel([entry('c'), entry('x'), entry('d'), entry('b'), entry('a')], null, null, null, links);
    assert.deepStrictEqual(logic.graphSessionOrder(model).map((node) => node.key), ['k-x', 'k-a', 'k-d', 'k-b', 'k-c']);
    const layout = logic.graphLayout(model);
    const y = (key) => layout.nodes.find((node) => node.key === key && node.kind === 'session').y;
    assert.ok(y('k-a') < y('k-b') && y('k-b') < y('k-c') && y('k-a') < y('k-d'));
});

test('lineage graph: a cycle is stacked once each and never loops', () => {
    const raw = new Map([['a', { parent: 'c', kind: 'started' }], ['b', { parent: 'a', kind: 'started' }], ['c', { parent: 'b', kind: 'started' }]]);
    const model = logic.graphModel([entry('b'), entry('a'), entry('c')], null, null, null, raw);
    assert.strictEqual(lineageEdges(model).length, 3);
    const order = logic.graphSessionOrder(model).map((node) => node.key);
    assert.deepStrictEqual(order, ['k-b', 'k-c', 'k-a']);
    assert.strictEqual(logic.graphLayout(model).nodes.length, 3);
});

test('lineage graph: the tooltip names both sessions, escaped', () => {
    const model = logic.graphModel([entry('a', { name: '<b>"Parent"</b>' }), entry('b')], null, null, null, lineage([['b', 'a', 'forked']]));
    const labels = { graph_lineage_started: '{child} started from {parent}', graph_lineage_forked: '{child} forked from {parent}' };
    const markup = logic.graphMarkup(logic.graphLayout(model), labels, null);
    assert.match(markup, /data-tip="Session b forked from &lt;b&gt;&quot;Parent&quot;&lt;\/b&gt;"/);
    assert.doesNotMatch(markup, /<b>"Parent"/);
    assert.match(markup, /class="edge edge-lineage forked"/);
});

test('lineage list: a child names its parent, a fork says so, and a parent counts what it started', () => {
    const labels = { lineage_started_from: 'Started from', lineage_forked_from: 'Forked from', lineage_children: 'Started from here',
        list_lineage_children: 'Started from here: {count}' };
    const names = new Map([['a', 'Parent'], ['b', 'Child']]);
    const nameOf = (id) => (names.has(id) ? names.get(id) : null);
    const links = lineage([['b', 'a'], ['c0000000-1111', 'a', 'forked'], ['a', 'root', 'forked']]);

    assert.strictEqual(logic.lineageMetaMarkup(logic.lineageOf(links, 'b'), nameOf, labels),
        '<span class="meta-lineage" data-tip="Started from Parent"><span class="meta-lineage-key">Started from</span> Parent</span>');
    const parent = logic.lineageMetaMarkup(logic.lineageOf(links, 'a'), nameOf, labels);
    assert.match(parent, /Forked from<\/span> root<\/span>/);
    assert.match(parent, /data-tip="Started from here\nChild\nc0000000">Started from here: 2<\/span>/);
    assert.strictEqual(logic.lineageMetaMarkup(logic.lineageOf(links, 'x'), nameOf, labels), '');
    assert.strictEqual(logic.lineageMetaMarkup(null, nameOf, labels), '');
});

test('lineage list: html safety - a hostile title or label stays text', () => {
    const links = lineage([['b', 'a']]);
    const markup = logic.lineageMetaMarkup(logic.lineageOf(links, 'b'), () => '<img src=x onerror=alert(1)>"', { lineage_started_from: '<b>' });
    assert.doesNotMatch(markup, /<img|<b>/);
    assert.match(markup, /&lt;img src=x onerror=alert\(1\)&gt;&quot;/);
});

test('lineage reveal plan: group and sub-heading in Urgency, section and project in Project', () => {
    const live = { session_id: 's', status: 'awaiting_input', alive: true, project_key: 'proj' };
    const view = { groups: [{ id: 'g', name: 'G' }], members: new Map([['s', 'g']]), archived: new Set(), pinned: new Set() };
    assert.deepStrictEqual(logic.lineageRevealPlan(live, view, 'urgency'), { section: null, group: 'group:g', sub: 'group:g|recent', project: null });
    assert.deepStrictEqual(logic.lineageRevealPlan(live, view, 'project'), { section: 'recent', group: null, sub: null, project: 'proj' });
    assert.deepStrictEqual(logic.lineageRevealPlan(live, {}, 'urgency'), { section: 'recent', group: null, sub: null, project: null });
    const pinned = Object.assign({}, view, { pinned: new Set(['s']) });
    assert.strictEqual(logic.lineageRevealPlan(live, pinned, 'urgency').group, 'pinned');
    const archived = { session_id: 's', status: 'completed', alive: false, is_history: true };
    assert.strictEqual(logic.lineageRevealPlan(archived, { archived: new Set(['s']) }, 'urgency').section, 'archived');
    assert.deepStrictEqual(logic.lineageRevealPlan(null, view, 'urgency'), { section: null, group: null, sub: null, project: null });
});

test('lineage graph: the legend shows both edges and the dimmed box', () => {
    const markup = logic.graphLegendMarkup({ graph_legend_started: 'Started', graph_legend_forked: 'Forked', graph_outside_window: 'Outside' });
    assert.match(markup, /edge edge-lineage"/);
    assert.match(markup, /edge edge-lineage forked"/);
    assert.match(markup, /gnode gnode-session dimmed/);
    for (const text of ['Started', 'Forked', 'Outside']) {
        assert.ok(markup.includes('<span>' + text + '</span>'), text);
    }
});
