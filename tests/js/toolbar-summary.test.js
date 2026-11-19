'use strict';

/* What the toolbar's summary counts, as render.js writes it, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('the toolbar counts only sessions whose process runs, and prices the same set', () => {
    // Ended sessions stay listed for a while; counting them under "Active"
    // made the number fall later with nothing active changing.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const element = { innerHTML: '' };
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__element = element;
    const run = (code) => vm.runInContext(code, context);
    run(`
        document = { getElementById: () => __element };
        setMarkup = (target, markup) => { target.innerHTML = markup; };
        state.labels = Object.assign({}, state.labels, { summary_active: 'Active: {count}' });
    `);
    const usage = { input_tokens: 1000000, output_tokens: 0 };
    const session = (id, alive) => ({ session_id: id, alive, model: 'claude-opus-4-8', usage, usage_by_model: { 'claude-opus-4-8': usage } });
    const prices = { 'opus-4-8': { input: 5, output: 25, cache_read: 0.5, cache_write_5m: 6.25, cache_write_1h: 10 } };
    context.__snapshot = { sessions: [session('a', true), session('b', true), session('c', false)] };
    context.__prices = prices;

    run('updateToolbarSummary(__snapshot, __prices);');

    assert.match(element.innerHTML, /^Active: <b>2<\/b>/);
    assert.match(element.innerHTML, /<b>\$10<\/b>$/);
});
