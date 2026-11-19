'use strict';

/* The details' transcript and scratchpad paths as detail.js asks for them, in
   the shared vm context the browser loads the scripts into (see
   global-scope.test.js), with the bridge answers settled by the test. */

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
    context.__answers = [];
    vm.runInContext(`
        renderDetail = () => {};
        transcriptPath = () => new Promise((resolve) => { __answers.push(resolve); });
        scratchpadPath = () => Promise.resolve('');
    `, context);

    return { context, run: (code) => vm.runInContext(code, context) };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('an older answer that lands last does not overwrite the newer one', async () => {
    // A new session has no transcript yet; once its first prompt lands the path
    // is asked again - and the first, empty answer may arrive after it.
    const { context, run } = controller();
    run("requestDetailPaths({ session_id: 's', pid: 1, cwd: 'c', status: 'new' });");
    run("requestDetailPaths({ session_id: 's', pid: 1, cwd: 'c', status: 'working' });");

    context.__answers[1]('C:/x/s.jsonl');
    await settle();
    context.__answers[0]('');
    await settle();

    assert.strictEqual(run('detailPaths.transcript'), 'C:/x/s.jsonl');
});
