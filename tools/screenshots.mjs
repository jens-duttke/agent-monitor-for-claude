// Agent Monitor for Claude - README screenshots (dev only, never shipped).
//
// Renders the UI's browser preview (index.html?mock, the invented sessions in
// ui/dev-mock.js) in a headless Edge or Chrome, driven over the DevTools
// protocol, and writes one PNG per scene to docs/images/. No dependency beyond
// Node 22 (built-in fetch and WebSocket) and an installed Chromium browser.
//
//   node tools/screenshots.mjs                 every scene
//   node tools/screenshots.mjs graph search    only these
//
// SCREENSHOT_BROWSER names the browser executable when none is found, or to use
// another one. The browser runs on a throw-away profile that is deleted after.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PAGE = pathToFileURL(join(REPO, 'agent_monitor_for_claude', 'ui', 'index.html')).href + '?mock';
const OUT_DIR = join(REPO, 'docs', 'images');
const PORT = 9333;
const SCALE = 2;

const BROWSERS = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
];

// The session every scene selects: the mock's richest one (processes,
// subagents, a workflow, a plan and tools).
const FOCUS_SESSION = 'h3c';

// The preview has no bridge, so no content scan ever answers. The search scene
// pushes this answer in by hand - invented excerpts in the exact shape
// search.py reports them.
const SEARCH_QUERY = 'fence';
const SEARCH_MATCHES = [
    {
        session_id: 'h3c', count: 14, partial: false, snippets: [
            { kind: 'assistant', before: 'The reload path signals the ', match: 'fence', after: ' before the old pipeline is retired, so the wait never returns.', clipped_after: true },
            { kind: 'tool_result', tool: 'Grep', before: 'src/vk/queue.cpp:212  vkWaitFor', match: 'Fence', after: 's(device, 1, &frameFence, VK_TRUE, UINT64_MAX);' },
            { kind: 'user', before: 'Can you check whether the ', match: 'fence', after: ' is reset twice when two shaders reload in one frame?' },
            { kind: 'thinking', before: 'If the swap happens between submit and the ', match: 'fence', after: ' wait, the next frame waits on a signal that already fired', clipped_after: true },
            { kind: 'tool_use', tool: 'Edit', before: 'lock(); swap(); reset', match: 'Fence', after: '(frameFence);' },
        ],
    },
    {
        session_id: 'h3f', count: 6, partial: false, snippets: [
            { kind: 'assistant', before: 'Regression appeared in 4.2: the ', match: 'fence', after: ' wait moved into the reload callback.' },
        ],
    },
    {
        session_id: 'h2p', count: 2, partial: false, snippets: [
            { kind: 'tool_result', tool: 'Bash', before: 'warning: pipeline cache written without a ', match: 'fence', after: ' - first frame may stall' },
        ],
    },
];

const SELECT_FOCUS = `selectSession(rowKey(state.last.sessions.find((s) => s.session_id === ${JSON.stringify(FOCUS_SESSION)})), false)`;

const SCENES = {
    async overview(page) {
        await page.open({}, 1440, 900);
        await page.run(SELECT_FOCUS);
        await page.shot('overview');
    },
    async graph(page) {
        await page.open({ 'amc-group': 'graph', 'amc-list-width': JSON.stringify({ graph: 1200 }) }, 1760, 960);
        await page.run(SELECT_FOCUS);
        await page.shot('graph');
    },
    async search(page) {
        await page.open({}, 1440, 900);
        await page.run(`(() => {
            const input = document.getElementById('search-input');
            input.focus();
            input.value = ${JSON.stringify(SEARCH_QUERY)};
            input.dispatchEvent(new Event('input', { bubbles: true }));
        })()`);
        await sleep(1200);
        await page.run(`window.__amcSearchPush({ seq: state.searchSeq, processed: 16, total: 16, done: true, matches: ${JSON.stringify(SEARCH_MATCHES)} })`);
        await page.run(SELECT_FOCUS);
        // A blinking caret would land in the picture or not, run by run.
        await page.run('document.activeElement.blur()');
        await page.shot('search');
    },
    async compact(page) {
        await page.open({ 'amc-compact': '1' }, 420, 640);
        await page.shot('compact');
    },
    async settings(page) {
        await page.open({}, 1440, 900);
        await page.run(SELECT_FOCUS);
        await page.run('openSettings()');
        await sleep(300);
        await page.run('document.activeElement.blur()');
        await page.shot('settings');
    },
};

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function findBrowser() {
    const chosen = process.env.SCREENSHOT_BROWSER;
    if (chosen) {
        return chosen;
    }
    const found = BROWSERS.find((path) => existsSync(path));
    if (!found) {
        throw new Error('No Edge or Chrome found - set SCREENSHOT_BROWSER to the browser executable.');
    }
    return found;
}

async function debuggerUrl() {
    for (let attempt = 0; attempt < 50; attempt++) {
        try {
            const targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
            const target = targets.find((entry) => entry.type === 'page');
            if (target) {
                return target.webSocketDebuggerUrl;
            }
        } catch (e) { /* the browser is still starting */ }
        await sleep(200);
    }
    throw new Error('The browser did not open its DevTools port.');
}

// One page, driven over the DevTools protocol.
async function connect() {
    const socket = new WebSocket(await debuggerUrl());
    await new Promise((done) => socket.addEventListener('open', done));

    let seq = 0;
    const pending = new Map();
    socket.addEventListener('message', (event) => {
        const message = JSON.parse(event.data);
        if (message.id && pending.has(message.id)) {
            pending.get(message.id)(message);
            pending.delete(message.id);
        }
    });
    const send = (method, params = {}) => {
        const id = ++seq;
        socket.send(JSON.stringify({ id, method, params }));
        return new Promise((done) => pending.set(id, done));
    };

    const run = async (expression) => {
        const reply = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
        const details = reply.result && reply.result.exceptionDetails;
        if (details) {
            throw new Error('Page script failed: ' + (details.exception ? details.exception.description : details.text));
        }
        return reply.result && reply.result.result ? reply.result.result.value : undefined;
    };

    const waitFor = async (expression) => {
        for (let attempt = 0; attempt < 50; attempt++) {
            if (await run(`(() => { try { return !!(${expression}); } catch (e) { return false; } })()`)) {
                return;
            }
            await sleep(100);
        }
        throw new Error('Timed out waiting for: ' + expression);
    };

    return {
        run,
        close: () => socket.close(),
        // Loads the preview at this size with exactly these UI preferences.
        async open(storage, width, height) {
            await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: SCALE, mobile: false });
            await send('Page.navigate', { url: PAGE });
            await waitFor('document.readyState === "complete"');
            const writes = Object.entries(storage).map(([key, value]) => `localStorage.setItem(${JSON.stringify(key)}, ${JSON.stringify(value)});`);
            await run('localStorage.clear();' + writes.join(''));
            await send('Page.reload');
            await sleep(300);
            await waitFor('state.last && state.last.sessions.length > 0');
        },
        // Lets the last render, the details' requests and any animation settle first.
        async shot(name) {
            await sleep(1000);
            const reply = await send('Page.captureScreenshot', { format: 'png' });
            writeFileSync(join(OUT_DIR, name + '.png'), Buffer.from(reply.result.data, 'base64'));
            console.log('wrote docs/images/' + name + '.png');
        },
    };
}

async function main() {
    const wanted = process.argv.slice(2);
    const unknown = wanted.filter((name) => !(name in SCENES));
    if (unknown.length) {
        throw new Error('Unknown scene: ' + unknown.join(', ') + ' (known: ' + Object.keys(SCENES).join(', ') + ')');
    }
    mkdirSync(OUT_DIR, { recursive: true });

    const profile = mkdtempSync(join(tmpdir(), 'amc-screenshots-'));
    const browser = spawn(findBrowser(), [
        '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
        '--hide-scrollbars', '--force-color-profile=srgb', '--no-first-run', 'about:blank',
    ], { stdio: 'ignore' });
    // A browser that cannot be started reports 'error' and never 'exit'.
    const exited = new Promise((done, fail) => {
        browser.once('exit', done);
        browser.once('error', fail);
    });

    try {
        const gone = exited.then(() => {
            throw new Error('The browser exited before it opened its DevTools port.');
        });
        const page = await Promise.race([connect(), gone]);
        try {
            for (const [name, scene] of Object.entries(SCENES)) {
                if (wanted.length === 0 || wanted.includes(name)) {
                    await scene(page);
                }
            }
        } finally {
            page.close();
        }
    } finally {
        browser.kill();
        await exited.catch(() => { /* already reported by the race above */ });
        rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
}

main().catch((err) => {
    console.error(err.message);
    process.exitCode = 1;
});
