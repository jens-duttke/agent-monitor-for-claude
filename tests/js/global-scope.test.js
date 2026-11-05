'use strict';

/* Guard for the one execution context no other test exercises: the browser,
   where logic.js and the controller scripts run as CLASSIC SCRIPTS sharing one
   global scope. Three failures live only there. A top-level function declaration
   in one file becomes a non-configurable global, and a later top-level
   `const`/`let` of the same name in another file is then a SyntaxError at
   script instantiation - that file never executes a single statement, the UI
   stays an empty shell, and nothing is logged (the error bridge lives in
   core.js itself). And a file whose top-level code reaches for a declaration
   made by a LATER file throws a ReferenceError at load. And the same function
   name declared at the top level of two files throws nothing: the later one
   replaces the earlier for every caller. Node's module wrapper gives every
   file its own scope, so logic.test.js can never catch any of them;
   this test recreates the browser's shared global with the vm module.

   The files are loaded in the order boot-scripts.js writes them, read from
   that file, so the test and the page can never disagree about it. Their top
   level only declares and registers listeners, so with a `window` stub each
   one must run to the end - any error at all fails the test. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

// Scripts in ui/ that are not part of the controller's load order: the two
// bootstraps themselves, and the browser preview's mock data, which only the
// preview loads (and which never ships).
const NOT_IN_LOAD_ORDER = new Set(['boot.js', 'boot-scripts.js', 'dev-mock.js']);

function loadOrder() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const list = source.match(/var scripts = \[([^\]]*)\]/);
    assert.ok(list, 'boot-scripts.js no longer holds a `var scripts = [...]` list');

    return [...list[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
}

test('the UI scripts share one browser global scope in their load order', () => {
    const registered = [];
    const window = { addEventListener: (type) => registered.push(type) };
    const context = vm.createContext({ window });

    for (const name of loadOrder()) {
        const source = fs.readFileSync(path.join(UI_DIR, name), 'utf8');
        try {
            vm.runInContext(source, context, { filename: name });
        } catch (err) {
            // The error comes from the vm context's own realm, so an
            // `instanceof` check against this realm's constructors is always
            // false - report the name instead.
            assert.fail(name + ' failed to load in a shared global scope: ' + (err && err.name) + ': ' + (err && err.message));
        }
    }

    // The last file got to run, and started the page the way the browser will.
    assert.strictEqual(vm.runInContext('typeof boot', context), 'function');
    assert.deepStrictEqual(registered.sort(), ['DOMContentLoaded', 'error', 'pywebviewready', 'unhandledrejection']);
});

// A function declared at the top level of two files is no error at all: the
// later file silently replaces the earlier one's, and every caller of the
// earlier one then runs the wrong function.
test('no top-level function is declared in two UI scripts', () => {
    const declaredIn = new Map();
    const duplicates = [];
    for (const name of loadOrder()) {
        const source = fs.readFileSync(path.join(UI_DIR, name), 'utf8');
        for (const match of source.matchAll(/^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/gm)) {
            const fn = match[1];
            if (declaredIn.has(fn)) {
                duplicates.push(fn + ' (' + declaredIn.get(fn) + ', ' + name + ')');
            } else {
                declaredIn.set(fn, name);
            }
        }
    }

    assert.deepStrictEqual(duplicates, []);
});

test('logic.js loads first and index.js last', () => {
    const order = loadOrder();

    assert.strictEqual(order[0], 'logic.js');
    assert.strictEqual(order[order.length - 1], 'index.js');
});

test('every controller script in ui/ is in the load order', () => {
    const listed = new Set(loadOrder());
    const unlisted = fs.readdirSync(UI_DIR)
        .filter((name) => name.endsWith('.js') && !NOT_IN_LOAD_ORDER.has(name) && !listed.has(name));

    assert.deepStrictEqual(unlisted, []);
});
