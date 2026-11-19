'use strict';

/* Starting a content search as the controller does it, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js), with
   render, the DOM updates and the bridge replaced by recorders. */

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
    context.__started = [];
    vm.runInContext(`
        render = () => {};
        updateSearchBox = () => {};
        updateSearchProgress = () => {};
        currentSessionRefs = () => [];
        apiBridge = () => ({ start_search: (query, refs, options, seq) => { __started.push({ query, seq }); return Promise.resolve(); } });
    `, context);

    return { context, run: (code) => vm.runInContext(code, context) };
}

test('a pattern only the backend engine knows is still searched', () => {
    // The page checks a regular expression with the JavaScript engine for
    // instant feedback, but Python's re decides: (?P<name>...) and an inline
    // flag are valid there and refused by JavaScript.
    for (const pattern of ['(?P<word>needle)', '(?i)needle']) {
        const { context, run } = controller();
        run(`state.searchRegex = true; state.search = ${JSON.stringify(pattern)}; runSearch();`);

        assert.deepStrictEqual(context.__started.map((call) => call.query), [pattern], pattern);
        // The backend's first answer without an error clears the provisional red box.
        run(`onSearchPush({ seq: state.searchSeq, processed: 0, total: 1, matches: [], done: false, error: false });`);
        assert.strictEqual(run('state.searchError'), false, pattern);
    }
});

test('a pattern both engines refuse stays flagged', () => {
    const { run } = controller();
    run(`state.searchRegex = true; state.search = '(unclosed'; runSearch();`);
    assert.strictEqual(run('state.searchError'), true);
    run(`onSearchPush({ seq: state.searchSeq, processed: 0, total: 0, matches: [], done: true, error: true });`);
    assert.strictEqual(run('state.searchError'), true);
});
