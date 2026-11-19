'use strict';

/* The background-task rows as proc-panel.js reconciles them, in the shared vm
   context the browser loads the scripts into (see global-scope.test.js), on a
   stand-in list element. */

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
    context.__created = [];
    vm.runInContext(`
        createTaskRow = (panel, task) => { __created.push(task.id); return { dataset: { taskId: task.id }, nextSibling: null, remove: () => {} }; };
        updateTaskRow = () => {};
    `, context);

    return (code) => vm.runInContext(code, context);
}

test('a task id named like an Object property gets a row of its own', () => {
    // An object lookup answered "constructor" with Object's own function,
    // which crashed the panel and wiped the list.
    const run = controller();
    run(`
        const list = { children: [], firstChild: null, insertBefore(node) { this.children.push(node); this.firstChild = this.children[0]; } };
        const el = { querySelector: (selector) => (selector === '.proc-task-list' ? list : { textContent: '', hidden: false }), hidden: false };
        globalThis.__panel = { el, expanded: new Set() };
    `);
    run(`renderTaskList(__panel, { tasks: [{ id: 'constructor', label: '', size: 1, age_seconds: 1 }, { id: '__proto__', label: '', size: 1, age_seconds: 1 }], total: 2 });`);

    assert.deepStrictEqual(Array.from(run('__created')), ['constructor', '__proto__']);
});
