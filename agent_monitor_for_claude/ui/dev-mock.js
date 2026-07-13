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
        child_count: 0, child_names: [], host: null, via_cli: false,
        has_transcript: true, has_activity: true, last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
        pending_tool: false, last_tool_name: null, usage_limited: false, permission_mode: null,
        model_id: null, usage: {}, usage_by_model: {}, model_timeline: [], title: null,
        subagents_running: 0, subagents_done: 0, subagents_labels: [], age_seconds: 0,
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
            child_count: 2, child_names: ['cmake.exe', 'helios-viewer.exe'],
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
            child_count: 1, child_names: ['helios-tests.exe'],
            age_seconds: 12,
            ...priced('claude-opus-4-8[1m]', {
                input_tokens: 62100, output_tokens: 14900, cache_read_input_tokens: 41200000,
                cache_creation_input_tokens: 1800000, cache_creation_5m_input_tokens: 1200000, cache_creation_1h_input_tokens: 600000,
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
            child_count: 1, child_names: ['argon2-bench.exe'],
            age_seconds: 27,
            ...priced('claude-fable-5', {
                input_tokens: 17200, output_tokens: 3600, cache_read_input_tokens: 11500000,
                cache_creation_input_tokens: 480000, cache_creation_5m_input_tokens: 480000, cache_creation_1h_input_tokens: 0,
            }),
        }),
    ],
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
