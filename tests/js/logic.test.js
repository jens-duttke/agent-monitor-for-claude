'use strict';

/* Node-based tests for the pure UI logic (no browser, no framework beyond the
   built-in node:test runner). Run: node --test tests/js  */

const test = require('node:test');
const assert = require('node:assert/strict');

const logic = require('../../agent_monitor_for_claude/ui/logic.js');

function raw(overrides) {
    return Object.assign({
        alive: true,
        has_transcript: true,
        last_stop_reason: null,
        pending_tool: false,
        pending_blocking: false,
        has_activity: true,
        last_entry_kind: null,
    }, overrides);
}

// A resolved price table (one schedule's worth), $/MTok, as resolvePrices returns.
const PRICES = {
    'opus-4-8':  { input: 5,  output: 25, cache_read: 0.50, cache_write_5m: 6.25,  cache_write_1h: 10 },
    'haiku-4-5': { input: 1,  output: 5,  cache_read: 0.10, cache_write_5m: 1.25,  cache_write_1h: 2 },
    'fable-5':   { input: 10, output: 50, cache_read: 1,    cache_write_5m: 12.50, cache_write_1h: 20 },
};

test('classify: dead process is completed', () => {
    assert.equal(logic.classify(raw({ alive: false, last_stop_reason: 'end_turn' })), 'completed');
});

test('classify: no transcript is new', () => {
    assert.equal(logic.classify(raw({ has_transcript: false, has_activity: false })), 'new');
});

test('classify: pending blocking awaits permission', () => {
    assert.equal(logic.classify(raw({ pending_tool: true, pending_blocking: true })), 'awaiting_permission');
});

test('classify: pending not blocking is working', () => {
    assert.equal(logic.classify(raw({ pending_tool: true, pending_blocking: false })), 'working');
});

test('classify: finished assistant turn awaits input', () => {
    assert.equal(logic.classify(raw({ last_entry_kind: 'assistant', last_stop_reason: 'end_turn' })), 'awaiting_input');
});

test('classify: a tool_result is working', () => {
    assert.equal(logic.classify(raw({ last_entry_kind: 'tool_result', last_stop_reason: 'tool_use' })), 'working');
});

test('classify: long silent thinking stays working', () => {
    // The user sent a prompt; the model thinks and writes nothing for minutes.
    // A stale user_text entry must NOT flip to "your turn".
    assert.equal(logic.classify(raw({ last_entry_kind: 'user_text', last_stop_reason: 'end_turn' })), 'working');
});

test('classify: a trailing local command is idle, not working', () => {
    // A slash/`!` command runs outside the model (Claude Code even tells the
    // model not to respond), so the newest turn being a local command owes no
    // reply - idle, not the stuck "working" a plain trailing user turn gives.
    assert.equal(logic.classify(raw({ last_entry_kind: 'local_command' })), 'awaiting_input');
});

test('classify: an interrupt marker is its own status, not working', () => {
    // The interrupt marker is a user turn on disk (like a fresh prompt) but
    // means control is back with you - a distinct "interrupted" state, never working.
    assert.equal(logic.classify(raw({ last_entry_kind: 'user_interrupt' })), 'interrupted');
});

test('classify: an interrupt overrides an unresolved pending tool', () => {
    // Interrupting mid-tool can leave a tool_use with no result; the trailing
    // interrupt marker still means the turn was stopped, so it wins over pending.
    assert.equal(logic.classify(raw({ last_entry_kind: 'user_interrupt', pending_tool: true, pending_blocking: true })), 'interrupted');
});

test('classify: mid-flight assistant turn is working', () => {
    assert.equal(logic.classify(raw({ last_entry_kind: 'assistant', last_stop_reason: 'tool_use' })), 'working');
});

test('classify: a trailing API error is its own status, not working', () => {
    // A usage/session limit (or overload/server error) stops the turn with a
    // non-end_turn assistant entry; nothing is running, so it is "errored",
    // never the "working" that entry would otherwise imply.
    assert.equal(logic.classify(raw({ last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence' })), 'errored');
});

test('classify: an API error overrides an unresolved pending tool', () => {
    // Like an interrupt, the error ends the turn even if it left a tool_use
    // unanswered, so it wins over the pending-tool rule.
    assert.equal(logic.classify(raw({ last_entry_kind: 'api_error', pending_tool: true, pending_blocking: true })), 'errored');
});

test('classify: unrecognized entry with activity awaits input', () => {
    assert.equal(logic.classify(raw({ last_entry_kind: null, last_stop_reason: null })), 'awaiting_input');
});

test('classify: empty transcript is unknown', () => {
    assert.equal(logic.classify(raw({ has_activity: false })), 'unknown');
});

test('refineWithNative', () => {
    assert.equal(logic.refineWithNative('awaiting_input', 'busy'), 'working');
    assert.equal(logic.refineWithNative('working', 'idle'), 'awaiting_input');
    assert.equal(logic.refineWithNative('new', 'idle'), 'new');
    // A native status - busy included - never demotes a fresh "new" session; New
    // has its own visibility toggle and must not be flipped out from under it.
    assert.equal(logic.refineWithNative('new', 'busy'), 'new');
    assert.equal(logic.refineWithNative('awaiting_permission', 'busy'), 'awaiting_permission');
    // `errored` is definitive (an API-error turn is newest, nothing is running),
    // so a lagging registry busy/idle must not flip or flatten it.
    assert.equal(logic.refineWithNative('errored', 'busy'), 'errored');
    assert.equal(logic.refineWithNative('errored', 'idle'), 'errored');
    assert.equal(logic.refineWithNative('working', null), 'working');
    // "waiting" with a reason is a blocking prompt the transcript has not caught up to.
    assert.equal(logic.refineWithNative('working', 'waiting', 'permission prompt'), 'awaiting_permission');
    assert.equal(logic.refineWithNative('new', 'waiting', 'permission prompt'), 'new');
    // A bare "waiting" with no reason is ambiguous - leave the structural classification alone.
    assert.equal(logic.refineWithNative('working', 'waiting', null), 'working');
});

test('filterBucket maps each status to its toolbar chip', () => {
    assert.equal(logic.filterBucket('awaiting_permission'), 'needs');
    assert.equal(logic.filterBucket('errored'), 'errored');
    assert.equal(logic.filterBucket('interrupted'), 'interrupted');
    assert.equal(logic.filterBucket('awaiting_input'), 'idle');
    assert.equal(logic.filterBucket('working'), 'working');
    assert.equal(logic.filterBucket('processing'), 'background');
    assert.equal(logic.filterBucket('completed'), 'quiet');
    assert.equal(logic.filterBucket('unknown'), 'quiet');
    assert.equal(logic.filterBucket('new'), 'new');
});

test('sessionBucket routes a history session to its own chip', () => {
    // A past session is completed but must land in the dedicated "history"
    // chip, not fold into "quiet" with the recently-ended ones.
    assert.equal(logic.sessionBucket({ status: 'completed', is_history: true }), 'history');
    assert.equal(logic.sessionBucket({ status: 'completed', is_history: false }), 'quiet');
    assert.equal(logic.sessionBucket({ status: 'working' }), 'working');
});

test('refineWithBackgroundWork', () => {
    assert.equal(logic.refineWithBackgroundWork('awaiting_input', true), 'processing');
    assert.equal(logic.refineWithBackgroundWork('unknown', true), 'processing');
    assert.equal(logic.refineWithBackgroundWork('awaiting_input', false), 'awaiting_input');
    assert.equal(logic.refineWithBackgroundWork('working', true), 'working');
    assert.equal(logic.refineWithBackgroundWork('awaiting_permission', true), 'awaiting_permission');
});

test('deriveStatus: thinking VS Code session (no native, no children) is working', () => {
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'user_text', last_stop_reason: 'end_turn', native_status: null, child_count: 0,
    }));
    assert.equal(status, 'working');
});

test('deriveStatus: finished turn with a running subagent is processing', () => {
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', subagents_running: 1, child_count: 0,
    }));
    assert.equal(status, 'processing');
});

test('deriveStatus: an interrupted session with a phantom subagent stays interrupted, not processing', () => {
    // The interrupt killed the in-process subagent; its still-"running" count is
    // a phantom the recent window has not cleared. It must not promote to processing.
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'user_interrupt', native_status: null, subagents_running: 1, child_count: 0,
    }));
    assert.equal(status, 'interrupted');
});

test('deriveStatus: an interrupted session survives a lagging native busy/idle', () => {
    // The interrupt marker is definitive - the registry catching up must neither
    // flip it to working (busy) nor flatten it to plain idle.
    assert.equal(logic.deriveStatus(raw({ last_entry_kind: 'user_interrupt', native_status: 'busy', child_count: 0 })), 'interrupted');
    assert.equal(logic.deriveStatus(raw({ last_entry_kind: 'user_interrupt', native_status: 'idle', child_count: 0 })), 'interrupted');
});

test('deriveStatus: an interrupted session with a surviving OS process still processes', () => {
    // A detached child process (a build or server) can outlive the interrupt, so
    // real background work still counts.
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'user_interrupt', native_status: null, subagents_running: 0, child_count: 1,
    }));
    assert.equal(status, 'processing');
});

test('deriveStatus: an errored session survives a lagging native busy/idle', () => {
    // The API-error turn is definitive - the registry catching up must neither
    // flip it to working (busy) nor flatten it to plain idle.
    assert.equal(logic.deriveStatus(raw({ last_entry_kind: 'api_error', native_status: 'busy', child_count: 0 })), 'errored');
    assert.equal(logic.deriveStatus(raw({ last_entry_kind: 'api_error', native_status: 'idle', child_count: 0 })), 'errored');
});

test('deriveStatus: an errored session with a phantom subagent stays errored, not processing', () => {
    // A usage limit stops the whole CLI, killing in-process subagents; the
    // still-"running" count is a phantom and must not promote to processing. The
    // error stays salient rather than being masked as background work.
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'api_error', native_status: null, subagents_running: 1, child_count: 0,
    }));
    assert.equal(status, 'errored');
});

test('deriveStatus: registry waiting-for-permission overrides a stale fresh prompt', () => {
    // The user answered a prompt; the tool_use for the pending permission has
    // not yet landed in the transcript, so the structural rule still sees the
    // fresh user prompt and would read "working". The registry knows it is blocked.
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'user_text', last_stop_reason: 'end_turn',
        native_status: 'waiting', waiting_for: 'permission prompt', child_count: 0,
    }));
    assert.equal(status, 'awaiting_permission');
});

test('deriveStatus: default-mode pending tool with idle process blocks on permission', () => {
    const status = logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Edit', permission_mode: 'default', child_count: 0,
    }));
    assert.equal(status, 'awaiting_permission');
});

test('deriveStatus: auto-mode pending tool is just working', () => {
    const status = logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Edit', permission_mode: 'auto', child_count: 0,
    }));
    assert.equal(status, 'working');
});

test('deriveStatus: a pending dialog tool blocks even with an unrelated child process', () => {
    // A dialog tool (question / plan review) never spawns a child, so a live
    // child alongside it is unrelated background work and must not demote the
    // block - the session is waiting on you, not busy. Holds in every mode.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'ExitPlanMode', permission_mode: 'default',
        child_count: 1, last_entry_kind: 'assistant',
    })), 'awaiting_permission');
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'AskUserQuestion', permission_mode: 'auto',
        child_count: 2, last_entry_kind: 'assistant',
    })), 'awaiting_permission');
});

test('deriveStatus: a generic pending tool with a child still reads as executing', () => {
    // A generic tool that spawned a child is running, not blocking - unchanged.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Bash', permission_mode: 'default',
        child_count: 1, last_entry_kind: 'assistant',
    })), 'working');
});

test('pendingIsBlocking', () => {
    assert.equal(logic.pendingIsBlocking('AskUserQuestion', 'auto'), true);
    assert.equal(logic.pendingIsBlocking('ExitPlanMode', 'acceptEdits'), true);
    assert.equal(logic.pendingIsBlocking('Edit', 'default'), true);
    assert.equal(logic.pendingIsBlocking('Edit', 'auto'), false);
});

test('attentionLabel', () => {
    const labels = {
        status_awaiting_permission: 'Permission needed',
        status_needs_you: 'Waiting for you',
        status_question: 'Question for you',
        status_plan_review: 'Plan review',
        status_working: 'Working',
        status_errored: 'Error',
        status_usage_limit: 'Usage limit reached',
    };
    // A known dialog tool keeps its precise label.
    assert.equal(logic.attentionLabel('awaiting_permission', 'AskUserQuestion', labels), 'Question for you');
    assert.equal(logic.attentionLabel('awaiting_permission', 'ExitPlanMode', labels), 'Plan review');
    // A real tool-permission prompt (tool known from the transcript) stays specific.
    assert.equal(logic.attentionLabel('awaiting_permission', 'Edit', labels), 'Permission needed');
    // The registry-derived block has no pending tool: stay neutral, do not claim permission.
    assert.equal(logic.attentionLabel('awaiting_permission', null, labels), 'Waiting for you');
    // An errored session names the usage limit specifically; any other error is generic.
    assert.equal(logic.attentionLabel('errored', null, labels, true), 'Usage limit reached');
    assert.equal(logic.attentionLabel('errored', null, labels, false), 'Error');
    // Other statuses fall through to their plain label.
    assert.equal(logic.attentionLabel('working', null, labels), 'Working');
});

test('pruneResumedHistory: a resumed session is not folded in twice', () => {
    const live = [{ session_id: 'a', alive: true }];
    const history = [{ session_id: 'a', is_history: true }, { session_id: 'b', is_history: true }];
    // Plain concat (the old behaviour) renders the resumed 'a' twice.
    assert.equal(live.concat(history).filter((s) => s.session_id === 'a').length, 2);
    // With the prune, the live session's stale history row is dropped.
    const folded = live.concat(logic.pruneResumedHistory(history, live));
    assert.equal(folded.filter((s) => s.session_id === 'a').length, 1);
    assert.deepEqual(logic.pruneResumedHistory(history, live).map((s) => s.session_id), ['b']);
    assert.deepEqual(logic.pruneResumedHistory([], live), []);
    assert.deepEqual(logic.pruneResumedHistory(history, []).map((s) => s.session_id), ['a', 'b']);
});

test('historyNeedsRefresh: true only when a previously-live session left the snapshot', () => {
    assert.equal(logic.historyNeedsRefresh([{ session_id: 'a' }, { session_id: 'b' }], [{ session_id: 'a' }]), true);
    assert.equal(logic.historyNeedsRefresh([{ session_id: 'a' }], [{ session_id: 'a' }]), false);
    assert.equal(logic.historyNeedsRefresh([{ session_id: 'a' }], []), true);
    // A brand-new session appearing is not a reason to refresh history.
    assert.equal(logic.historyNeedsRefresh([{ session_id: 'a' }], [{ session_id: 'a' }, { session_id: 'c' }]), false);
    assert.equal(logic.historyNeedsRefresh([], [{ session_id: 'a' }]), false);
});

test('sessionMatchesSearch: a not-yet-run search (null matches) shows everything', () => {
    const set = new Set(['a']);
    assert.equal(logic.sessionMatchesSearch('a', false, null), true);       // no query
    assert.equal(logic.sessionMatchesSearch('a', true, null), true);        // query pending in debounce -> show all
    assert.equal(logic.sessionMatchesSearch('a', true, set), true);         // matched
    assert.equal(logic.sessionMatchesSearch('z', true, set), false);        // not matched
    assert.equal(logic.sessionMatchesSearch('z', true, new Set()), false);  // ran, matched nothing -> hide
});

test('searchScopeRefs: the delta scope skips dead sessions the full scope keeps', () => {
    const live = { session_id: 'a', cwd: 'd:\\a', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' };
    const dead = { session_id: 'b', cwd: 'd:\\b', alive: false, has_transcript: true, has_activity: true };
    const filters = new Set(['working', 'quiet']);

    // Full search: a dead-but-visible (quiet) session is read once.
    const full = logic.searchScopeRefs([live, dead], null, filters, true).map((r) => r.session_id).sort();
    assert.deepEqual(full, ['a', 'b']);

    // Delta rescan: dead sessions are skipped (append-only, cannot gain a match).
    const delta = logic.searchScopeRefs([live, dead], null, filters, false).map((r) => r.session_id);
    assert.deepEqual(delta, ['a']);
});

test('searchScopeRefs: history is only in scope for the full search with its chip on', () => {
    const live = { session_id: 'a', cwd: 'd:\\a', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' };
    const history = [{ session_id: 'h', cwd: 'd:\\h', alive: false, is_history: true }];

    const full = logic.searchScopeRefs([live], history, new Set(['working', 'history']), true).map((r) => r.session_id).sort();
    assert.deepEqual(full, ['a', 'h']);

    // The delta never folds in history at all.
    const delta = logic.searchScopeRefs([live], history, new Set(['working', 'history']), false).map((r) => r.session_id);
    assert.deepEqual(delta, ['a']);

    // History chip off: history is out of scope even for the full search.
    const chipOff = logic.searchScopeRefs([live], history, new Set(['working']), true).map((r) => r.session_id);
    assert.deepEqual(chipOff, ['a']);
});

test('defaultFilterKeys: excludes off-by-default chips (the history scan opt-out)', () => {
    const defs = [
        { key: 'needs' },
        { key: 'idle' },
        { key: 'history', offByDefault: true },
    ];
    assert.deepEqual(logic.defaultFilterKeys(defs), ['needs', 'idle']);
    assert.equal(logic.defaultFilterKeys(defs).includes('history'), false);
    assert.deepEqual(logic.defaultFilterKeys(), []);
});

test('settleCall: a synchronous throw runs onError, not the caller', () => {
    const errors = [];
    const ret = logic.settleCall(() => { throw new Error('sync'); }, (e) => errors.push(e.message));
    assert.deepEqual(errors, ['sync']);
    assert.equal(ret, undefined);
});

test('settleCall: an async rejection is contained and runs onError', async () => {
    const errors = [];
    await logic.settleCall(() => Promise.reject(new Error('async')), (e) => errors.push(e.message));
    assert.deepEqual(errors, ['async']);
});

test('settleCall: a resolved or plain call does not run onError', async () => {
    const errors = [];
    await logic.settleCall(() => Promise.resolve('ok'), (e) => errors.push(e));
    assert.equal(logic.settleCall(() => 42, (e) => errors.push(e)), 42);
    assert.deepEqual(errors, []);
});

test('settleCall: an onError that itself throws is swallowed', async () => {
    // Cleanup must never re-throw and re-enter the global unhandledrejection path.
    await logic.settleCall(() => Promise.reject(new Error('x')), () => { throw new Error('cleanup boom'); });
});

test('modeLabel', () => {
    assert.equal(logic.modeLabel('default'), 'Manual');
    assert.equal(logic.modeLabel('acceptEdits'), 'Auto-edit');
    assert.equal(logic.modeLabel(null), null);
});

test('formatAgeSince: advances the age from its capture epoch', () => {
    const labels = { age_seconds: '{s}s', age_minutes: '{m}m', age_hours: '{h}h{m}', age_days: '{d}d' };
    const now = 1_000_000_000_000;
    // Captured just now: shows the base.
    assert.equal(logic.formatAgeSince(30, now, now, labels), '30s');
    // Captured an hour ago: a 30s base has advanced by 3600s -> ~1h.
    assert.equal(logic.formatAgeSince(30, now - 3600_000, now, labels), '1h0');
    // A history row captured long ago keeps growing, not frozen at the base:
    const frozen = logic.formatAge(120, labels);            // what a pinned age would show
    const advanced = logic.formatAgeSince(120, now - 172800_000, now, labels); // +2 days
    assert.notEqual(advanced, frozen);
    assert.equal(advanced, '2d');
    // Null base is empty; a non-finite capture epoch formats the base as-is.
    assert.equal(logic.formatAgeSince(null, now, now, labels), '');
    assert.equal(logic.formatAgeSince(45, NaN, now, labels), '45s');
});

test('formatModel', () => {
    assert.equal(logic.formatModel('claude-opus-4-8[1m]'), 'Opus 4.8 1M');
    assert.equal(logic.formatModel('claude-haiku-4-5-20251001'), 'Haiku 4.5');
    assert.equal(logic.formatModel('claude-fable-5'), 'Fable 5');
    assert.equal(logic.formatModel(null), null);
});

test('formatTokens', () => {
    assert.equal(logic.formatTokens(950), '950');
    assert.equal(logic.formatTokens(12400), '12.4k');
    assert.equal(logic.formatTokens(120000), '120k');
    assert.equal(logic.formatTokens(3100000), '3.1M');
});

test('formatTokens rounds cleanly across tier boundaries', () => {
    // A value that rounds up across a boundary must promote, not overflow the tier.
    assert.equal(logic.formatTokens(999500), '1.0M');   // not "1000k"
    assert.equal(logic.formatTokens(999999), '1.0M');
    assert.equal(logic.formatTokens(99999), '100k');    // not "100.0k"
    assert.equal(logic.formatTokens(99955), '100k');
    assert.equal(logic.formatTokens(99949), '99.9k');   // just below the boundary
    assert.equal(logic.formatTokens(100000), '100k');
    assert.equal(logic.formatTokens(1000000), '1.0M');
});

test('tokenLabels: empty usage yields no label', () => {
    assert.equal(logic.tokenLabels(null, {}), '');
    assert.equal(logic.tokenLabels({ input_tokens: 0, output_tokens: 0 }, {}), '');
});

test('tokenLabels splits cache writes by TTL and shows only the non-zero parts', () => {
    const labels = {
        token_summary: '{input} in · {output} out',
        token_cache_read: '{cache_read} cache read',
        token_cache_5m: '{cache_5m} 5m write',
        token_cache_1h: '{cache_1h} 1h write',
        token_cache_write: '{cache_write} cache write',
    };
    // No cache: just fresh input/output.
    assert.equal(logic.tokenLabels({ input_tokens: 2, output_tokens: 123 }, labels), '2 in · 123 out');
    // Only the cache parts that occur are shown (here: 1h write only).
    assert.equal(
        logic.tokenLabels({ input_tokens: 2, output_tokens: 123, cache_creation_input_tokens: 32976, cache_creation_1h_input_tokens: 32976 }, labels),
        '2 in · 123 out · 33.0k 1h write',
    );
    // Hits plus both write TTLs, each its own pricing-aligned figure.
    assert.equal(
        logic.tokenLabels({
            input_tokens: 599, output_tokens: 473000, cache_read_input_tokens: 55000000,
            cache_creation_input_tokens: 3300000, cache_creation_5m_input_tokens: 1200000, cache_creation_1h_input_tokens: 2100000,
        }, labels),
        '599 in · 473k out · 55.0M cache read · 1.2M 5m write · 2.1M 1h write',
    );
    // Legacy turn without the TTL split: the unattributed writes fall back to a combined figure.
    assert.equal(
        logic.tokenLabels({ input_tokens: 10, output_tokens: 20, cache_creation_input_tokens: 5000 }, labels),
        '10 in · 20 out · 5.0k cache write',
    );
});

test('modelPriceKey normalizes an id to its pricing key', () => {
    assert.equal(logic.modelPriceKey('claude-opus-4-8[1m]'), 'opus-4-8');
    assert.equal(logic.modelPriceKey('claude-haiku-4-5-20251001'), 'haiku-4-5');
    assert.equal(logic.modelPriceKey('claude-sonnet-4-6'), 'sonnet-4-6');
    assert.equal(logic.modelPriceKey('claude-fable-5'), 'fable-5');
    // 3.x ids put the version before the family, so the key differs in shape.
    assert.equal(logic.modelPriceKey('claude-3-5-haiku-20241022'), '3-5-haiku');
    assert.equal(logic.modelPriceKey(null), null);
});

test('every real model id derives to a key that pricing.json actually contains', () => {
    const pricing = require('../../pricing.json');
    const ids = ['claude-opus-4-8[1m]', 'claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-3-5-haiku-20241022'];
    for (const [date, table] of Object.entries(pricing)) {
        if (date === '_comment') {
            continue;
        }
        for (const id of ids) {
            const key = logic.modelPriceKey(id);
            assert.ok(key in table, `${id} -> ${key} missing from pricing.json schedule ${date}`);
        }
    }
});

test('resolvePrices picks the schedule effective on the date', () => {
    const schedules = {
        '1970-01-01': { 'sonnet-5': { input: 2 } },
        '2026-09-01': { 'sonnet-5': { input: 3 } },
    };
    assert.equal(logic.resolvePrices(schedules, '2026-07-12')['sonnet-5'].input, 2);
    assert.equal(logic.resolvePrices(schedules, '2026-09-01')['sonnet-5'].input, 3);   // boundary is inclusive
    assert.equal(logic.resolvePrices(schedules, '2027-01-01')['sonnet-5'].input, 3);
    // Before any schedule, or no schedules at all -> empty (everything falls back to tokens).
    assert.deepEqual(logic.resolvePrices(schedules, '1969-01-01'), {});
    assert.deepEqual(logic.resolvePrices({}, '2026-07-12'), {});
});

test('resolvePrices returns a schedule verbatim: a partial schedule drops other models', () => {
    // The docs pricing example must list every model per date - a schedule fully
    // replaces the previous one (no merge), so a model omitted from the active
    // schedule loses its cost estimate once that date takes effect.
    const schedules = {
        '1970-01-01': { 'opus-4-8': { output: 25 }, 'sonnet-5': { output: 10 } },
        '2026-09-01': { 'sonnet-5': { output: 15 } },  // partial: opus-4-8 omitted
    };
    const active = logic.resolvePrices(schedules, '2026-09-02');
    assert.equal('opus-4-8' in active, false);
    assert.equal(logic.sessionCostUsd({ 'claude-opus-4-8': { output_tokens: 1000000 } }, active), null);
    assert.equal(logic.sessionCostUsd({ 'claude-sonnet-5': { output_tokens: 1000000 } }, active), 15);
});

test('usageCostUsd prices each token class with the model rate', () => {
    const rate = { input: 5, output: 25, cache_read: 0.5, cache_write_5m: 6.25, cache_write_1h: 10 };
    const perMillion = (u) => logic.usageCostUsd(u, rate);
    assert.equal(perMillion({ input_tokens: 1000000 }), 5);
    assert.equal(perMillion({ output_tokens: 1000000 }), 25);
    assert.equal(perMillion({ cache_read_input_tokens: 1000000 }), 0.5);
    assert.equal(perMillion({ cache_creation_5m_input_tokens: 1000000, cache_creation_input_tokens: 1000000 }), 6.25);
    assert.equal(perMillion({ cache_creation_1h_input_tokens: 1000000, cache_creation_input_tokens: 1000000 }), 10);
    // Un-split writes (a total without a TTL breakdown) are priced at the 5m rate.
    assert.equal(perMillion({ cache_creation_input_tokens: 1000000 }), 6.25);
});

test('sessionCostUsd sums per model, and bails to null on an unpriced model', () => {
    // A 1h cache write on Opus 4.8 ($10/MTok) dominates even a tiny in/out turn (~$0.33).
    const real = { 'claude-opus-4-8': {
        input_tokens: 2, output_tokens: 123, cache_read_input_tokens: 0,
        cache_creation_input_tokens: 32976, cache_creation_5m_input_tokens: 0, cache_creation_1h_input_tokens: 32976,
    } };
    assert.equal(logic.formatCost(logic.sessionCostUsd(real, PRICES)), '<$1');
    // Opus main + Haiku subagent are each priced at their own rate.
    const mixed = {
        'claude-opus-4-8': { output_tokens: 1000000 },
        'claude-haiku-4-5': { output_tokens: 1000000 },
    };
    assert.equal(logic.sessionCostUsd(mixed, PRICES), 30);   // 25 + 5
    // A zero-usage model (e.g. a synthetic placeholder) adds no cost and must not force a fallback.
    assert.equal(logic.sessionCostUsd({
        'claude-opus-4-8': { output_tokens: 1000000 },
        '<synthetic>': { input_tokens: 0, output_tokens: 0 },
    }, PRICES), 25);
    // A model absent from the price table but with real usage makes the total unavailable.
    assert.equal(logic.sessionCostUsd({ 'claude-mythos-5': { output_tokens: 1000 } }, PRICES), null);
    assert.equal(logic.sessionCostUsd({}, PRICES), null);
});

test('formatCost is whole dollars, <$1 below a dollar, null passes through', () => {
    assert.equal(logic.formatCost(19.27), '$19');
    assert.equal(logic.formatCost(19.7), '$20');       // rounded to the nearest dollar
    assert.equal(logic.formatCost(1), '$1');
    assert.equal(logic.formatCost(0.998535), '<$1');
    assert.equal(logic.formatCost(0), '<$1');
    assert.equal(logic.formatCost(null), null);
});

test('buildSession: compact is the cost when priced, else a token total', () => {
    const labels = {
        token_summary: '{input} in · {output} out',
        token_cache_1h: '{cache_1h} 1h write',
    };
    const usage = { input_tokens: 2, output_tokens: 123, cache_creation_input_tokens: 32976, cache_creation_1h_input_tokens: 32976 };
    const base = {
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage,
    };
    // Priced (Opus 4.8): compact is the estimated cost; the detail is the breakdown
    // that slides open before it, so expanded reads "<breakdown> · <compact>".
    const priced = logic.buildSession({ ...base, model_id: 'claude-opus-4-8',
        usage_by_model: { 'claude-opus-4-8': usage } }, labels, PRICES);
    assert.equal(priced.usage_compact, '<$1');   // ~$0.33
    assert.equal(priced.usage_detail, '2 in · 123 out · 33.0k 1h write ·\u00A0');
    // Unpriced model (absent from the table): compact falls back to the token total.
    const unpriced = logic.buildSession({ ...base, model_id: 'claude-mythos-5',
        usage_by_model: { 'claude-mythos-5': usage } }, labels, PRICES);
    assert.equal(unpriced.usage_compact, logic.formatTokens(2 + 123 + 0 + 32976));
    assert.equal(unpriced.usage_detail, '2 in · 123 out · 33.0k 1h write ·\u00A0');
});

test('buildSession: an interrupt reads as interrupted and clears the phantom subagent badge', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'user_interrupt', native_status: null, usage: {},
        subagents_running: 1, subagents_labels: ['general-purpose'], child_count: 0,
    }, { status_interrupted: 'Interrupted' }, {});
    assert.equal(session.status, 'interrupted');
    assert.equal(session.status_label, 'Interrupted');
    assert.equal(session.subagents_running, 0);
    assert.deepEqual(session.subagents_labels, []);
});

test('buildSession: a usage limit reads as errored, is named specifically, and clears the phantom subagent badge', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence', usage_limited: true,
        native_status: null, usage: {}, subagents_running: 1, subagents_labels: ['general-purpose'], child_count: 0,
    }, { status_errored: 'Error', status_usage_limit: 'Usage limit reached' }, {});
    assert.equal(session.status, 'errored');
    assert.equal(session.status_label, 'Usage limit reached');
    assert.equal(session.needs_attention, true);
    assert.equal(session.subagents_running, 0);
    assert.deepEqual(session.subagents_labels, []);
});

test('buildSession: a history record is completed, flagged, and content-free', () => {
    // A past session is dead (alive:false) with no usage; it must derive to
    // completed, carry the is_history flag through for the row styling/menu,
    // and show no cost or token figure.
    const session = logic.buildSession({
        session_id: 'aaaaaaaa-1111-2222-3333-444444444444', is_history: true, alive: false,
        has_transcript: true, cwd: 'd:\\x', short_name: 'aaaaaaaa', title: 'Old work',
        model_id: 'claude-opus-4-8', usage: {}, usage_by_model: {},
    }, { status_completed: 'Finished' }, PRICES);
    assert.equal(session.status, 'completed');
    assert.equal(session.is_history, true);
    assert.equal(session.name, 'Old work');
    assert.equal(session.usage_compact, '');
});

test('buildSession: a non-limit API error reads as errored with the generic label', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence', usage_limited: false,
        native_status: null, usage: {}, child_count: 0,
    }, { status_errored: 'Error', status_usage_limit: 'Usage limit reached' }, {});
    assert.equal(session.status, 'errored');
    assert.equal(session.status_label, 'Error');
});

test('buildSession: a registry-waiting block with a stale tool name stays neutral', () => {
    // Blocked on you via the registry `waiting` route (no pending tool in the
    // transcript yet), but an earlier, already-resolved tool left last_tool_name
    // set. The label must stay neutral, not name that stale, resolved tool.
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'user_text', last_stop_reason: 'end_turn', pending_tool: false,
        last_tool_name: 'AskUserQuestion', native_status: 'waiting', waiting_for: 'permission prompt',
        usage: {}, child_count: 0,
    }, {
        status_needs_you: 'Waiting for you', status_question: 'Question for you',
        status_awaiting_permission: 'Permission needed', status_plan_review: 'Plan review',
    }, {});
    assert.equal(session.status, 'awaiting_permission');
    assert.equal(session.status_label, 'Waiting for you');
});

test('buildSession: a genuinely pending dialog tool keeps its specific label', () => {
    // With a real pending tool, the name is meaningful and the precise label wins.
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', pending_tool: true, last_tool_name: 'AskUserQuestion',
        permission_mode: 'default', native_status: null, usage: {}, child_count: 0,
    }, { status_question: 'Question for you', status_needs_you: 'Waiting for you' }, {});
    assert.equal(session.status, 'awaiting_permission');
    assert.equal(session.status_label, 'Question for you');
});

test('modelHistory preserves the backend order and formats labels', () => {
    // A switch back to a prior model is a distinct run, so it appears again as
    // the last entry - the timeline is already ordered, modelHistory must not
    // reorder or dedupe it.
    const timeline = [
        { time: '2026-07-11T09:25:21Z', model: 'claude-opus-4-8' },
        { time: '2026-07-11T11:08:48Z', model: 'claude-fable-5' },
        { time: '2026-07-11T14:58:11Z', model: 'claude-sonnet-5' },
        { time: '2026-07-11T16:30:42Z', model: 'claude-opus-4-8' },
    ];
    const history = logic.modelHistory(timeline);
    assert.deepEqual(history.map((e) => e.label), ['Opus 4.8', 'Fable 5', 'Sonnet 5', 'Opus 4.8']);
    assert.equal(history[0].time, '2026-07-11T09:25:21Z');
    assert.equal(history[history.length - 1].time, '2026-07-11T16:30:42Z');
    assert.deepEqual(logic.modelHistory([]), []);
    assert.deepEqual(logic.modelHistory(null), []);
});

test('buildSession flags a model switch and builds the history', () => {
    const base = {
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage: {}, model_id: 'claude-opus-4-8',
    };
    // Switched to Fable and back to Opus: three runs, last one the current model.
    const switched = logic.buildSession({ ...base,
        model_timeline: [
            { time: '2026-07-11T09:00:00Z', model: 'claude-opus-4-8' },
            { time: '2026-07-11T12:00:00Z', model: 'claude-fable-5' },
            { time: '2026-07-11T15:00:00Z', model: 'claude-opus-4-8' },
        ],
    }, {}, {});
    assert.equal(switched.model, 'Opus 4.8');
    assert.equal(switched.model_switched, true);
    assert.deepEqual(switched.model_history.map((e) => e.label), ['Opus 4.8', 'Fable 5', 'Opus 4.8']);
    // One model -> no "(+)".
    const single = logic.buildSession({ ...base,
        model_timeline: [{ time: '2026-07-11T09:00:00Z', model: 'claude-opus-4-8' }] }, {}, {});
    assert.equal(single.model_switched, false);
});

test('grouping helpers', () => {
    assert.equal(logic.groupKey('d:\\WebDev\\proj'), logic.groupKey('D:\\WebDev\\proj'));
    assert.equal(logic.displayCwd('d:\\WebDev\\proj'), 'D:\\WebDev\\proj');
    assert.equal(logic.projectName('D:\\WebDev\\oku3d-app'), 'oku3d-app');
});

test('modelRank orders by capability, unknown last', () => {
    assert.ok(logic.modelRank('Haiku 4.5') < logic.modelRank('Opus 4.8'));
    assert.equal(logic.modelRank(null), logic.MODEL_RANK.length);
});

test('STATUS_ORDER: needs -> working -> background -> errored -> idle -> interrupted -> quiet', () => {
    // Priority ranking for sorting within a project: blocked-on-you first, then
    // the session still doing work (foreground, then background), then a stuck
    // errored turn, then the calm states - finished-idle, interrupted, and the
    // terminal ones last.
    const order = logic.STATUS_ORDER;
    assert.ok(order.awaiting_permission < order.working);
    assert.ok(order.working < order.processing);
    assert.ok(order.processing < order.errored);
    assert.ok(order.errored < order.awaiting_input);
    assert.ok(order.awaiting_input < order.interrupted);
    assert.ok(order.interrupted < order.completed);
    assert.ok(order.interrupted < order.unknown);
});

test('displayCwd coerces a non-string cwd instead of throwing', () => {
    assert.equal(logic.displayCwd('c:\\proj'), 'C:\\proj');   // drive letter upper-cased
    assert.equal(logic.displayCwd('/usr/local'), '/usr/local');
    // Hardening: a non-string cwd must not throw (groupProjects would blank the view).
    assert.equal(logic.displayCwd(null), '');
    assert.equal(logic.displayCwd(undefined), '');
    assert.equal(logic.displayCwd(123), '123');
});

test('groupProjects groups case-insensitively', () => {
    const labels = { status_awaiting_input: 'Idle' };
    const projects = logic.groupProjects([
        raw({ cwd: 'd:\\WebDev\\proj', session_id: 'a', short_name: 'a', last_entry_kind: 'assistant', last_stop_reason: 'end_turn' }),
        raw({ cwd: 'D:\\WebDev\\proj', session_id: 'b', short_name: 'b', last_entry_kind: 'assistant', last_stop_reason: 'end_turn' }),
    ], labels);
    assert.equal(projects.length, 1);
    assert.equal(projects[0].sessions.length, 2);
    assert.equal(projects[0].sessions[0].status_label, 'Idle');
});

function project(name, statuses) {
    return { name, cwd: 'd:\\repos\\' + name, sessions: statuses.map((status) => ({ status })) };
}

test('projectBand: most urgent session wins, unknown/empty falls to the quiet band', () => {
    assert.equal(logic.projectBand([{ status: 'working' }, { status: 'awaiting_permission' }]), 0);
    assert.equal(logic.projectBand([{ status: 'working' }, { status: 'processing' }]), 1);
    assert.equal(logic.projectBand([{ status: 'completed' }, { status: 'new' }]), 2);
    assert.equal(logic.projectBand([]), 2);
});

test('projectBand: a finished "your turn" (awaiting_input) is quiet, not top', () => {
    // Only a truly blocked session (awaiting_permission) is top-band; a finished
    // turn sinks below the projects still doing work.
    assert.equal(logic.projectBand([{ status: 'awaiting_input' }]), 2);
    assert.equal(logic.projectBand([{ status: 'awaiting_input' }, { status: 'working' }]), 1);
});

test('projectBand: an interrupted session is quiet (your turn), not top', () => {
    // Interrupted is "your turn" like idle - nothing is running or mandatory, so
    // it stays in the quiet band and does not pull its project up.
    assert.equal(logic.projectBand([{ status: 'interrupted' }]), 2);
    assert.equal(logic.projectBand([{ status: 'interrupted' }, { status: 'working' }]), 1);
});

test('projectBand: an errored session is quiet, not top', () => {
    // A stuck-on-error session cannot proceed on its own, but there is often
    // nothing to do but wait for the limit to reset - so it stays quiet-band and
    // does not pull its project above the ones still doing work. Its own colour
    // and chip already make it stand out within the panel.
    assert.equal(logic.projectBand([{ status: 'errored' }]), 2);
    assert.equal(logic.projectBand([{ status: 'errored' }, { status: 'working' }]), 1);
});

test('sortProjects: priority order groups by band, then alphabetically within a band', () => {
    const projects = [
        project('zeta', ['working']),
        project('alpha', ['completed']),
        project('beta', ['awaiting_input']),
        project('gamma', ['awaiting_permission']),
    ];
    const names = logic.sortProjects(projects, true).map((p) => p.name);
    // band 0 (gamma, blocked), then band 1 (zeta, working), then band 2 (alpha,
    // beta - finished/idle) alphabetically.
    assert.deepEqual(names, ['gamma', 'zeta', 'alpha', 'beta']);
});

test('sortProjects: fine-grained churn inside a band does not reorder panels', () => {
    const before = logic.sortProjects([project('build', ['working']), project('app', ['processing'])], true);
    assert.deepEqual(before.map((p) => p.name), ['app', 'build']);
    // app flips working <-> processing (same busy band): order must be unchanged.
    const after = logic.sortProjects([project('build', ['working']), project('app', ['working'])], true);
    assert.deepEqual(after.map((p) => p.name), ['app', 'build']);
});

test('sortProjects: without priority it is a plain alphabetical list', () => {
    const projects = [project('zeta', ['awaiting_permission']), project('alpha', ['completed'])];
    assert.deepEqual(logic.sortProjects(projects, false).map((p) => p.name), ['alpha', 'zeta']);
});

test('sortProjects: does not mutate its input', () => {
    const projects = [project('b', ['completed']), project('a', ['completed'])];
    logic.sortProjects(projects, true);
    assert.deepEqual(projects.map((p) => p.name), ['b', 'a']);
});
