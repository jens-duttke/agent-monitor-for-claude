'use strict';

/* The app's tooltip as tooltip.js drives it, in the shared vm context the
   browser loads the scripts into (see global-scope.test.js), on a stand-in
   document whose listeners the test calls and whose timer it runs. */

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const UI_DIR = path.join(__dirname, '..', '..', 'agent_monitor_for_claude', 'ui');

function controller() {
    const source = fs.readFileSync(path.join(UI_DIR, 'boot-scripts.js'), 'utf8');
    const names = [...source.match(/var scripts = \[([^\]]*)\]/)[1].matchAll(/'([\w.-]+\.js)'/g)].map((match) => match[1]);
    const timers = [];
    const listeners = {};
    const element = () => {
        const classes = new Set();
        return {
            classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name) },
            style: {}, textContent: '', isConnected: true, contains: () => false, getBoundingClientRect: () => ({ left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 }),
            appendChild() {}, setAttribute() {}, addEventListener() {},
        };
    };
    const window = { addEventListener: () => {}, innerWidth: 1000, innerHeight: 800 };
    const context = vm.createContext({ window, setTimeout: (fn) => timers.push(fn), clearTimeout: () => {} });
    for (const name of names) {
        vm.runInContext(fs.readFileSync(path.join(UI_DIR, name), 'utf8'), context, { filename: name });
    }
    context.document = {
        createElement: element, body: element(), elementFromPoint: () => null,
        addEventListener: (type, handler) => { listeners[type] = handler; },
    };
    vm.runInContext('initTooltips();', context);
    const tip = vm.runInContext('tooltipEl', context);

    return { timers, listeners, tip, context, run: (code) => vm.runInContext(code, context) };
}

function trigger(tip) {
    return { dataset: { tip }, isConnected: true, closest() { return this; }, contains: () => false, getBoundingClientRect: () => ({ left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 }) };
}

test('a tooltip still shows when a render replaced its target during the delay', () => {
    // The timer checked the node it was armed for, which the render had
    // swapped for an equal one - nothing showed for the rest of the hover.
    const { timers, listeners, tip, context } = controller();
    const before = trigger('Cost of the session');
    listeners.pointerover({ target: before, clientX: 5, clientY: 5 });
    const after = trigger('Cost of the session');
    before.isConnected = false;
    context.document.elementFromPoint = () => after;
    context.syncTooltip();
    timers.forEach((fn) => fn());

    assert.ok(tip.classList.contains('show'));
});

test('a scroll that cannot have moved the target leaves the tooltip up', () => {
    // The task console follows its output every second; that scroll hid every
    // tooltip in the window.
    const { timers, listeners, tip } = controller();
    const target = trigger('Compact view');
    listeners.pointerover({ target, clientX: 5, clientY: 5 });
    timers.forEach((fn) => fn());
    listeners.scroll({ target: { contains: () => false } });

    assert.ok(tip.classList.contains('show'));
});
