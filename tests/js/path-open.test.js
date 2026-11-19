'use strict';

/* How often a click on a path launches the file manager, as actions.js routes
   it, in the shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function loadScripts() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__calls = [];
    vm.runInContext(`apiBridge = () => ({
        open_path: (cwd) => { __calls.push('open ' + cwd); return Promise.resolve(true); },
        reveal_path: (file) => { __calls.push('reveal ' + file); return Promise.resolve(true); },
    });`, context);
    return context;
}

// A click event on an element matching exactly one selector.
function clickOn(selector, dataset, detail) {
    return { detail, target: { closest: (wanted) => (wanted === selector ? { dataset } : null) } };
}

test('a double-click on a path opens its folder once', () => {
    // Both clicks of a double-click fire `click`; each launched the folder.
    const context = loadScripts();
    for (const handler of ['onContentClick', 'onDetailClick']) {
        context.__calls.length = 0;
        context.__handler = vm.runInContext(handler, context);
        for (const detail of [1, 2]) {
            context.__handler(clickOn('.path-open', { cwd: '/work', origin: 'local' }, detail));
        }
        assert.deepStrictEqual([...context.__calls], ['open /work'], handler);
    }
});

test('a double-click on a reveal button shows the file once, and the keyboard still works', () => {
    const context = loadScripts();
    const handler = vm.runInContext('onDetailClick', context);
    for (const detail of [1, 2, 0]) {
        handler(clickOn('[data-reveal-path]', { revealPath: '/t.jsonl', origin: 'local' }, detail));
    }
    assert.deepStrictEqual([...context.__calls], ['reveal /t.jsonl', 'reveal /t.jsonl']);
});
