'use strict';

/* The cached Invocation block of a background process, as detail.js keeps it,
   in the shared vm context the browser loads the scripts into (see
   global-scope.test.js), with the bridge answers given by the test. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller(answers) {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__answers = answers;
    context.__asked = 0;
    vm.runInContext(`
        renderDetail = () => {};
        callProcessDetails = () => { __asked += 1; return Promise.resolve(__answers.shift()); };
    `, context);

    return { context, run: (code) => vm.runInContext(code, context) };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const SESSION = "{ session_id: 's', pid: 1, origin: 'local' }";

test('a refused answer is asked for again instead of kept for good', async () => {
    const { context, run } = controller([null, { pid: 7, command_line: ['make'], started_at: null }]);
    run(`requestProcessInfo(${SESSION}, { pid: 7, name: 'make' });`);
    await settle();
    // A moment later the bridge can answer - a registry not yet re-read, a
    // process whose handle could not be opened yet.
    run(`processInfo.failedAt.set(processInfoKey(${SESSION}, 7), Date.now() - PROCESS_INFO_RETRY_MS - 1);`);
    run(`requestProcessInfo(${SESSION}, { pid: 7, name: 'make' });`);
    await settle();

    assert.strictEqual(context.__asked, 2);
    assert.deepStrictEqual(run(`processInfoOf(${SESSION}, { pid: 7, name: 'make' }).command_line`), ['make']);
});

test('a pid reused by another process does not show the ended one\'s invocation', async () => {
    const { context, run } = controller([{ pid: 7, command_line: ['old'], started_at: 1000 }, { pid: 7, command_line: ['new'], started_at: 2000 }]);
    run(`requestProcessInfo(${SESSION}, { pid: 7, name: 'node' });`);
    await settle();

    // Same pid, another program: the cached entry is not this process's.
    assert.strictEqual(run(`processInfoOf(${SESSION}, { pid: 7, name: 'python' })`), null);
    run(`requestProcessInfo(${SESSION}, { pid: 7, name: 'python' });`);
    await settle();
    assert.strictEqual(context.__asked, 2);
    assert.deepStrictEqual(run(`processInfoOf(${SESSION}, { pid: 7, name: 'python' }).command_line`), ['new']);
});
