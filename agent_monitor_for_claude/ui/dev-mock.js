'use strict';

/* Agent Monitor for Claude - browser preview data (dev only).

   index.js loads this file only when the UI is opened directly in a browser
   (a file:// page, or ?mock), where there is no pywebview bridge to supply real
   data. It is deliberately NOT listed in the PyInstaller spec, so it never ships
   in the packaged app: the shipped binary, always served over http with the
   bridge present, has no code path that reads fabricated session data.

   Everything below is INVENTED showcase content for screenshots and UI
   iteration - the project paths, session titles and process names are fictional
   and never describe a real session.

   Two deliberate non-duplications keep this from drifting: there are no labels
   here (the full English set lives in index.js DEFAULT_LABELS), and the pricing
   block is only a small snapshot needed to demo the cost line - it does not
   mirror the whole shipped pricing.json, and a stale figure only nudges a demo
   dollar amount, never correctness. */

// The only bootstrap fields the UI reads beyond labels: poll cadence, the
// default-effort badge, and the price schedules (date-keyed, exactly like
// pricing.json - resolvePrices picks the latest date on or before today).
window.__MOCK_BOOTSTRAP__ = {
    poll_interval: 5,
    default_effort: 'xhigh',
    pricing: {
        '1970-01-01': {
            'opus-4-8': { input: 5, output: 25, cache_read: 0.50, cache_write_5m: 6.25, cache_write_1h: 10 },
            'sonnet-5': { input: 2, output: 10, cache_read: 0.20, cache_write_5m: 2.50, cache_write_1h: 4 },
            'haiku-4-5': { input: 1, output: 5, cache_read: 0.10, cache_write_5m: 1.25, cache_write_1h: 2 },
            'fable-5': { input: 10, output: 50, cache_read: 1, cache_write_5m: 12.50, cache_write_1h: 20 },
        },
    },
};

// Raw session records - the exact shape the Python backend provides. All
// derivation (status, labels, grouping, sorting, cost) happens in logic.js.
function rawSession(overrides) {
    return Object.assign({
        pid: 0, session_id: '', cwd: 'D:\\Projects\\helios-renderer', short_name: '', kind: 'interactive',
        entrypoint: null, native_status: null, waiting_for: null, alive: true,
        child_count: 0, host: null, via_cli: false,
        has_transcript: true, has_activity: true, last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
        pending_tool: false, last_tool_name: null, usage_limited: false, permission_mode: null,
        model_id: null, usage: {}, usage_by_model: {}, model_timeline: [], title: null,
        subagents_running: 0, subagents_done: 0, subagents_labels: [], workflows: [], age_seconds: 0,
    }, overrides);
}

// A single-model session: the per-model cost split is just the overall usage
// under that one model id. Spread into a rawSession() call so usage is written
// once and both the breakdown (usage) and the cost (usage_by_model) stay in sync.
function priced(modelId, usage) {
    return { model_id: modelId, usage: usage, usage_by_model: { [modelId]: usage } };
}

window.__MOCK_SNAPSHOT__ = {
    generated_at: new Date().toISOString(),
    sessions: [
        // --- Helios - real-time spectral path tracer (GPU renderer) ---
        rawSession({
            session_id: 'h1a', short_name: 'helios-renderer-b0', pid: 24880, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Add spectral upsampling to the BRDF sampler', permission_mode: 'default',
            last_stop_reason: 'tool_use', pending_tool: true, last_tool_name: 'AskUserQuestion',
            age_seconds: 38,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 96400, output_tokens: 18200, cache_read_input_tokens: 58300000,
                cache_creation_input_tokens: 2100000, cache_creation_5m_input_tokens: 1400000, cache_creation_1h_input_tokens: 500000,
            }),
        }),
        rawSession({
            session_id: 'h2b', short_name: 'helios-renderer-7e', pid: 13360, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Port the ReSTIR denoiser to compute shaders', permission_mode: 'acceptEdits',
            last_entry_kind: 'tool_result', last_stop_reason: 'tool_use',
            child_count: 2,
            age_seconds: 6,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 41800, output_tokens: 9600, cache_read_input_tokens: 33500000,
                cache_creation_input_tokens: 1400000, cache_creation_5m_input_tokens: 950000, cache_creation_1h_input_tokens: 450000,
            }),
        }),
        rawSession({
            session_id: 'h3c', short_name: 'helios-renderer-c1', pid: 30512, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Hunt down the shader hot-reload race', permission_mode: 'auto',
            subagents_running: 4, subagents_done: 6,
            subagents_labels: [
                'Bisect the frame where the GPU fence deadlocks',
                'Reproduce descriptor-set corruption under vsync',
                'Diff SPIR-V output across a hot reload',
                'Audit the pipeline-cache invalidation path',
            ],
            child_count: 1,
            age_seconds: 12,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 62100, output_tokens: 14900, cache_read_input_tokens: 41200000,
                cache_creation_input_tokens: 1800000, cache_creation_5m_input_tokens: 1200000, cache_creation_1h_input_tokens: 600000,
            }),
        }),

        // A background workflow. Its badge shows the run's total agent count (12),
        // read from the workflow journal - stable across the fan-out phases where
        // no single agent is momentarily running, so the row no longer flickers
        // between "Background" and "Idle". The turn itself ended (end_turn), yet
        // the still-active workflow keeps it reading as "Background".
        rawSession({
            session_id: 'h3f', short_name: 'helios-shader-audit', pid: 30980, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Deep-research the shader hot-reload regression', permission_mode: 'auto',
            subagents_running: 6,
            subagents_labels: [
                'workflow-subagent', 'workflow-subagent', 'workflow-subagent',
                'workflow-subagent', 'workflow-subagent', 'workflow-subagent',
            ],
            workflows: [{ run_id: 'wf_a63b7dde', total: 12, done: 4, active: true }],
            age_seconds: 34,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 38000, output_tokens: 8200, cache_read_input_tokens: 28000000,
                cache_creation_input_tokens: 1100000, cache_creation_5m_input_tokens: 700000, cache_creation_1h_input_tokens: 400000,
            }),
        }),

        // A session that switched models mid-run - shows the model column's "(+)".
        rawSession({
            session_id: 'h4d', short_name: 'helios-renderer-2f', pid: 21030, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Retune the importance sampler across models', permission_mode: 'default',
            last_entry_kind: 'assistant', last_stop_reason: 'end_turn', age_seconds: 210,
            model_id: 'claude-opus-4-8[1m]',
            usage: {
                input_tokens: 3600, output_tokens: 244000, cache_read_input_tokens: 512000000,
                cache_creation_input_tokens: 24000000, cache_creation_5m_input_tokens: 9000000, cache_creation_1h_input_tokens: 15000000,
            },
            usage_by_model: {
                'claude-opus-4-8[1m]': { input_tokens: 2000, output_tokens: 180000, cache_read_input_tokens: 380000000, cache_creation_input_tokens: 15000000, cache_creation_5m_input_tokens: 6000000, cache_creation_1h_input_tokens: 9000000 },
                'claude-fable-5': { input_tokens: 1400, output_tokens: 58000, cache_read_input_tokens: 120000000, cache_creation_input_tokens: 8000000, cache_creation_5m_input_tokens: 2800000, cache_creation_1h_input_tokens: 5200000 },
                'claude-sonnet-5': { input_tokens: 200, output_tokens: 6000, cache_read_input_tokens: 12000000, cache_creation_input_tokens: 1000000, cache_creation_5m_input_tokens: 200000, cache_creation_1h_input_tokens: 800000 },
            },
            model_timeline: [
                { time: '2026-07-11T09:25:21Z', model: 'claude-opus-4-8[1m]' },
                { time: '2026-07-11T11:08:48Z', model: 'claude-fable-5' },
                { time: '2026-07-11T14:58:11Z', model: 'claude-sonnet-5' },
                { time: '2026-07-11T16:30:42Z', model: 'claude-opus-4-8[1m]' },
            ],
        }),

        // A turn the user stopped: its own yellow "Interrupted" status. The two
        // subagents it was running die with the interrupt, so their badge is
        // suppressed (never shown next to an interrupted session).
        rawSession({
            session_id: 'h5e', short_name: 'helios-renderer-9a', pid: 26744, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Refactor the BVH builder for wide nodes', permission_mode: 'default',
            last_entry_kind: 'user_interrupt', last_stop_reason: null,
            subagents_running: 2, subagents_labels: ['Benchmark the 8-wide traversal kernel', 'Port the SAH split to SoA'],
            age_seconds: 320,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 28800, output_tokens: 6400, cache_read_input_tokens: 19600000,
                cache_creation_input_tokens: 720000, cache_creation_5m_input_tokens: 520000, cache_creation_1h_input_tokens: 200000,
            }),
        }),

        // A turn that stopped when the account hit its usage/session limit: its
        // own orange "Usage limit reached" status. Nothing is running and the
        // model cannot continue until the limit resets, so it is never mistaken
        // for a still-"Working" session. The last real model is still shown.
        rawSession({
            session_id: 'h6f', short_name: 'helios-renderer-d4', pid: 28190, entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Vectorize the tone-mapping pass', permission_mode: 'default',
            last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence', usage_limited: true,
            age_seconds: 5400,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 52300, output_tokens: 11800, cache_read_input_tokens: 34700000,
                cache_creation_input_tokens: 1300000, cache_creation_5m_input_tokens: 900000, cache_creation_1h_input_tokens: 400000,
            }),
        }),

        // --- Aurora - collaborative CRDT engine (real-time presence) ---
        rawSession({
            session_id: 'a1d', short_name: 'aurora-realtime-34', pid: 18204, cwd: 'D:\\Projects\\aurora-realtime',
            entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Redesign the presence protocol as binary frames', permission_mode: 'plan',
            last_stop_reason: 'tool_use', pending_tool: true, last_tool_name: 'ExitPlanMode',
            age_seconds: 22,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 12300, output_tokens: 3400, cache_read_input_tokens: 8900000,
                cache_creation_input_tokens: 640000, cache_creation_5m_input_tokens: 640000, cache_creation_1h_input_tokens: 0,
            }),
        }),
        rawSession({
            session_id: 'a2e', short_name: 'aurora-realtime-9f', pid: 22976, cwd: 'D:\\Projects\\aurora-realtime',
            entrypoint: 'cli', host: 'Windows Terminal', via_cli: true,
            title: 'Fix cursor drift on high-latency connections',
            last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
            age_seconds: 2460,
            ...priced('claude-sonnet-5', {
                input_tokens: 28700, output_tokens: 6100, cache_read_input_tokens: 19400000,
                cache_creation_input_tokens: 820000, cache_creation_5m_input_tokens: 300000, cache_creation_1h_input_tokens: 520000,
            }),
        }),
        rawSession({
            session_id: 'a3f', short_name: 'aurora-realtime-4f', pid: 27640, cwd: 'D:\\Projects\\aurora-realtime',
            entrypoint: 'claude-vscode', host: 'VS Code',
            has_transcript: false, has_activity: false, last_entry_kind: null, last_stop_reason: null, age_seconds: 15,
        }),

        // --- Nimbus - distributed edge API gateway ---
        rawSession({
            session_id: 'n1g', short_name: 'nimbus-gateway-1c', pid: 15188, cwd: 'D:\\Projects\\nimbus-gateway',
            entrypoint: 'cli', host: 'PowerShell', via_cli: true,
            title: 'Implement adaptive token-bucket rate limiting', permission_mode: 'default',
            native_status: 'busy', last_entry_kind: 'user_text', last_stop_reason: 'end_turn',
            age_seconds: 4,
            ...priced('claude-sonnet-5', {
                input_tokens: 21500, output_tokens: 4300, cache_read_input_tokens: 14100000,
                cache_creation_input_tokens: 560000, cache_creation_5m_input_tokens: 560000, cache_creation_1h_input_tokens: 0,
            }),
        }),
        rawSession({
            session_id: 'n2h', short_name: 'nimbus-gateway-8a', pid: 20132, cwd: 'D:\\Projects\\nimbus-gateway',
            entrypoint: 'cli', host: 'Windows Terminal', via_cli: true,
            title: 'Wire mTLS between edge nodes', permission_mode: 'default',
            last_entry_kind: 'user_text', last_stop_reason: 'end_turn',
            native_status: 'waiting', waiting_for: 'permission prompt',
            age_seconds: 11,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 9800, output_tokens: 1900, cache_read_input_tokens: 4200000,
                cache_creation_input_tokens: 310000, cache_creation_5m_input_tokens: 310000, cache_creation_1h_input_tokens: 0,
            }),
        }),

        // --- Cipher - zero-knowledge secrets vault ---
        rawSession({
            session_id: 'c1i', short_name: 'cipher-vault-2d', pid: 12704, cwd: 'D:\\Projects\\cipher-vault',
            entrypoint: 'cli', host: 'Windows Terminal', via_cli: true,
            title: 'Constant-time comparison audit', alive: false,
            last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
            age_seconds: 10800,
            ...priced('claude-haiku-4-5-20251001', {
                input_tokens: 34600, output_tokens: 7800, cache_read_input_tokens: 22700000,
                cache_creation_input_tokens: 900000, cache_creation_5m_input_tokens: 400000, cache_creation_1h_input_tokens: 500000,
            }),
        }),
        rawSession({
            session_id: 'c2j', short_name: 'cipher-vault-a5', pid: 29456, cwd: 'D:\\Projects\\cipher-vault', kind: 'background',
            entrypoint: 'claude-vscode', host: 'VS Code',
            title: 'Sweep Argon2id memory-hardness parameters', permission_mode: 'acceptEdits',
            last_entry_kind: 'tool_result', last_stop_reason: 'tool_use',
            child_count: 1,
            age_seconds: 27,
            ...priced('claude-fable-5', {
                input_tokens: 17200, output_tokens: 3600, cache_read_input_tokens: 11500000,
                cache_creation_input_tokens: 480000, cache_creation_5m_input_tokens: 480000, cache_creation_1h_input_tokens: 0,
            }),
        }),
    ],
};

// --- process panel preview (get_process_stats / get_tasks / read_task_output) ---
//
// The panel polls these once a second while it is open. Values here are INVENTED
// and driven off the wall clock so the browser preview feels live (Date.now and
// Math.random are fine in a browser preview, unlike workflow scripts); a thunk is
// called on each poll. Keyed exactly like the real bridge: process stats by pid,
// tasks by session id, output by task id.
const __secs = () => Math.floor(Date.now() / 1000);
const __wiggle = (base, spread) => Math.max(0, base + (Math.random() - 0.5) * spread);
const __ramp = (span) => __secs() % span;

window.__MOCK_PROC_STATS__ = {
    // helios-renderer-7e: a CMake build feeding a live viewer.
    13360: () => ([
        { pid: 21804, name: 'cmake.exe', cpu: __wiggle(38, 22), rss: 214 * 1024 * 1024, uptime: 6 + __ramp(600) },
        { pid: 22190, name: 'helios-viewer.exe', cpu: __wiggle(12, 8), rss: 486 * 1024 * 1024, uptime: 6 + __ramp(600) },
    ]),
    // helios-renderer-c1: a single long-running test binary.
    30512: () => ([
        { pid: 18664, name: 'helios-tests.exe', cpu: __wiggle(64, 20), rss: 158 * 1024 * 1024, uptime: 12 + __ramp(600) },
    ]),
    // cipher-vault: an Argon2 sweep running under WSL - the Windows-side relays
    // read as idle, and the real load shows up as the shared WSL2-VM row.
    29456: () => ([
        { pid: 27200, name: 'wsl.exe', cpu: 0, rss: 13 * 1024 * 1024, uptime: 27 + __ramp(600) },
        { pid: 27210, name: 'bash.exe', cpu: 0, rss: 8 * 1024 * 1024, uptime: 27 + __ramp(600) },
        { pid: 9000, name: 'vmmemWSL', cpu: __wiggle(180, 40), rss: 7.3 * 1024 * 1024 * 1024, uptime: null, kind: 'wsl_vm' },
    ]),
};

window.__MOCK_TASKS__ = {
    h2b: [
        { id: 'a7f3k9', size: 4200, age: 1, label: 'Build spectral upsampling in the background' },
    ],
    h3c: [
        { id: 'r1n8xq', size: 91800, age: 2, label: 'Run the ReSTIR frame regression suite' },
        { id: 'w2k5vt', size: 640, age: 420, label: 'Warm up the shader cache' },
    ],
    c2j: [
        { id: 'b3n1lm', size: 12600, age: 0, label: 'Sweep Argon2id memory-hardness parameters' },
    ],
};

window.__MOCK_TASK_OUTPUT__ = {
    a7f3k9: () => [
        '-- 3rdparty/ --',
        '[  8%] Building CXX object src/spectral/upsample.cpp.o',
        '[ 12%] Building CXX object src/spectral/brdf.cpp.o',
        '[ 17%] Building CXX object src/restir/reservoir.cpp.o',
        '[ ' + (20 + __ramp(40)) + '%] Building CXX object src/restir/denoise.cpp.o',
    ].join('\n'),
    r1n8xq: () => [
        // ANSI SGR codes so the preview shows the console's colorization.
        '\x1b[1m== native edge264 MT (-kb) ==\x1b[0m',
        '\x1b[32mPASS\x1b[0m  \x1b[33mUNSUPPORTED\x1b[0m  \x1b[31mFAIL\x1b[0m',
        'seq  frame  0  Yhash=ad56e60a1ea1a6c2  \x1b[32mOK\x1b[0m',
        'seq  frame  1  Yhash=b5bc2ac56f81ef12  \x1b[32mOK\x1b[0m',
        '  [prefix_base] MVCRP_2: ' + (120 + __ramp(130)) + '/250 frames…',
    ].join('\n'),
    w2k5vt: 'setup complete.\nwaiting for input…\n',
    b3n1lm: () => [
        'argon2id  m=262144  t=3  p=4',
        'hash: 41.2 ms  (target < 50 ms)',
        'sweep ' + (14 + __ramp(18)) + '/32 parameter sets done',
    ].join('\n'),
};

// Sessions whose row menu should offer "Open scratchpad" - a non-empty path is
// all the check needs (opening it is a no-op without a bridge).
window.__MOCK_SCRATCHPADS__ = {
    h2b: 'D:\\Projects\\helios-renderer\\.scratch',
    h3c: 'D:\\Projects\\helios-renderer\\.scratch',
    c2j: 'D:\\Projects\\cipher-vault\\.scratch',
};

// Past, non-live sessions - the on-demand history listing (get_history). All are
// dead (alive:false, is_history:true) with no usage, exactly like the backend's
// history records. index.js loads these only when the history chip is enabled.
function historySession(overrides) {
    return rawSession(Object.assign({
        alive: false, is_history: true, pid: null, host: null, entrypoint: null,
        last_entry_kind: null, last_stop_reason: null, permission_mode: null,
        usage: {}, usage_by_model: {},
    }, overrides));
}

window.__MOCK_HISTORY__ = [
    historySession({
        session_id: 'aaaaaaaa-1111-2222-3333-444444444444', short_name: 'aaaaaaaa',
        cwd: 'D:\\Projects\\helios-renderer', title: 'Prototype the wavefront path-tracing loop',
        model_id: 'claude-opus-4-8[1m]', age_seconds: 172800,
    }),
    historySession({
        session_id: 'bbbbbbbb-1111-2222-3333-444444444444', short_name: 'bbbbbbbb',
        cwd: 'D:\\Projects\\helios-renderer', title: 'First pass at the material system',
        model_id: 'claude-sonnet-5', age_seconds: 604800,
    }),
    historySession({
        session_id: 'cccccccc-1111-2222-3333-444444444444', short_name: 'cccccccc',
        cwd: 'D:\\Projects\\atlas-cli', title: 'Scaffold the argument parser',
        model_id: 'claude-haiku-4-5', age_seconds: 1209600,
    }),
    historySession({
        session_id: 'dddddddd-1111-2222-3333-444444444444', short_name: 'dddddddd',
        cwd: 'D:\\Projects\\atlas-cli', title: null, age_seconds: 2592000,
    }),
];
