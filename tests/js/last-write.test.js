'use strict';

/* When the details ask for a session's media again, as detail.js decides it,
   in the shared vm context the browser loads the scripts into (see
   global-scope.test.js). */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('an unchanged transcript is not asked for again on every poll', async () => {
    // The last write was derived from a floored age and the page's own clock,
    // so it flipped between two seconds with nothing written.
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {} }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.__asked = 0;
    const run = (code) => vm.runInContext(code, context);
    run(`
        renderDetail = () => {};
        apiBridge = () => ({ get_session_media: () => { __asked += 1; return Promise.resolve({ items: [], files: [] }); } });
    `);
    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    // The newest entry was written at second 1000; three polls read it.
    for (const [pythonAt, pageAt] of [[1100.4, 1100.6], [1101.7, 1101.9], [1102.2, 1102.3]]) {
        const raw = { session_id: 's', pid: 1, cwd: 'c', alive: true, has_transcript: true, has_activity: true,
            last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage: {}, child_count: 0, age_seconds: pythonAt - 1000 };
        run(`state.receivedAt = ${pageAt * 1000};`);
        context.__session = run(`logic.buildSession(${JSON.stringify(raw)}, state.labels, {})`);
        run('requestMedia(__session, null);');
        await settle();
    }

    assert.strictEqual(context.__asked, 1);
});
