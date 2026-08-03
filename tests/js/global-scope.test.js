'use strict';

/* Guard for the one execution context no other test exercises: the browser,
   where logic.js and index.js run as CLASSIC SCRIPTS sharing one global
   scope. A top-level function declaration in logic.js becomes a
   non-configurable global; a later top-level `const`/`let` of the same name
   in index.js is then a SyntaxError at script instantiation - index.js never
   executes a single statement, the UI stays an empty shell, and nothing is
   logged (the error bridge lives inside index.js itself). Node's module
   wrapper gives every file its own scope, so logic.test.js can never catch
   this; this test recreates the browser's shared global with the vm module.

   The scripts are only INSTANTIATED here, not expected to run to completion:
   index.js touches the DOM at top level, so a ReferenceError for `document`
   (or any other missing browser global) is fine. The one failure mode this
   guard exists for is the SyntaxError thrown before execution starts. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

test('logic.js and index.js can share one browser global scope', () => {
    const context = vm.createContext({});

    for (const name of ['logic.js', 'index.js']) {
        const source = fs.readFileSync(path.join(UI_DIR, name), 'utf8');
        try {
            vm.runInContext(source, context, { filename: name });
        } catch (err) {
            // The error comes from the vm context's own realm, so an
            // `instanceof SyntaxError` check against this realm's constructor
            // is always false - compare the name instead.
            assert.notStrictEqual(
                (err || {}).name, 'SyntaxError',
                name + ' failed script instantiation in a shared global scope: ' + (err && err.message)
            );
            // Any runtime error (no document, no window) means instantiation
            // succeeded and execution started - exactly what the browser needs.
            break;
        }
    }
});
