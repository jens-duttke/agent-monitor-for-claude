'use strict';

/* Opening the context menu as menu.js does, in the shared vm context the
   browser loads the scripts into (see global-scope.test.js), on a stand-in
   document. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const context = vm.createContext({ window: { addEventListener: () => {}, removeEventListener: () => {}, innerWidth: 1000, innerHeight: 800 }, setTimeout, clearTimeout });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    const element = () => ({
        classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, children: [],
        appendChild(child) { this.children.push(child); return child; }, addEventListener() {}, remove() {}, setAttribute() {},
        querySelectorAll: () => [], querySelector: () => ({ textContent: '' }), focus() {},
        getBoundingClientRect: () => ({ left: 0, top: 0, right: 100, bottom: 20, width: 100, height: 20 }),
    });
    context.__element = element;
    context.document = { createElement: element, body: element(), addEventListener() {}, removeEventListener() {} };

    return (code) => vm.runInContext(code, context);
}

test('a second right-click on the same row opens the menu again', () => {
    // A contextmenu event does not reach the outside-click listener first, so
    // the toggle meant for a button closed the menu instead of moving it.
    const run = controller();
    run(`globalThis.__row = __element();
        openMenu(__row, [{ action: 'copy', label: 'Copy' }], () => {}, { type: 'row', point: { x: 5, y: 5 } });
        openMenu(__row, [{ action: 'copy', label: 'Copy' }], () => {}, { type: 'row', point: { x: 50, y: 50 } });`);
    assert.ok(run('openMenuState !== null'));
});
