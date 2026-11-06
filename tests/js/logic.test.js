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

test('classify: a turn older than the process holding the session is idle, not working', () => {
    // The session was reopened after an earlier process ended mid-turn: the
    // reply that process owed, the call it left open and the dialog it showed
    // all died with it, and this process has not taken a turn yet - so the
    // session waits for you, whatever the dangling entry says.
    assert.equal(logic.classify(raw({ last_entry_kind: 'user_text', turn_predates_process: true })), 'awaiting_input');
    assert.equal(logic.classify(raw({ last_entry_kind: 'tool_result', last_stop_reason: 'tool_use', turn_predates_process: true })), 'awaiting_input');
    assert.equal(logic.classify(raw({ last_entry_kind: 'assistant', last_stop_reason: 'tool_use', turn_predates_process: true })), 'awaiting_input');
    assert.equal(logic.classify(raw({ pending_tool: true, pending_blocking: true, turn_predates_process: true })), 'awaiting_input');
});

test('classify: a reopen never demotes the definitive stops, a fresh window or an ended process', () => {
    // An interrupt is still an interrupt after a reopen, an error still an
    // error, a window with no transcript still new, and a dead process finished.
    assert.equal(logic.classify(raw({ last_entry_kind: 'user_interrupt', turn_predates_process: true })), 'interrupted');
    assert.equal(logic.classify(raw({ last_entry_kind: 'api_error', turn_predates_process: true })), 'errored');
    assert.equal(logic.classify(raw({ has_transcript: false, has_activity: false, turn_predates_process: true })), 'new');
    assert.equal(logic.classify(raw({ alive: false, turn_predates_process: true })), 'completed');
});

test('turnPredatesProcess: fires only on a turn clearly older than the live process', () => {
    const at = (overrides) => logic.turnPredatesProcess(raw(overrides));

    assert.equal(at({ age_seconds: 400000, process_age_seconds: 60000 }), true);
    assert.equal(at({ age_seconds: '400000', process_age_seconds: '60000' }), true);
    // Within the slack the turn and the process start belong to one start-up.
    assert.equal(at({ age_seconds: 100 + logic.PROCESS_START_SLACK_SECONDS, process_age_seconds: 100 }), false);
    assert.equal(at({ age_seconds: 100, process_age_seconds: 100 }), false);
    assert.equal(at({ age_seconds: 50, process_age_seconds: 100 }), false);
    // A transcript with no turn has nothing to predate.
    assert.equal(at({ has_activity: false, age_seconds: 400000, process_age_seconds: 60000 }), false);
    // A missing or unusable age on either side is no evidence - and null must
    // not read as a zero-second process age (Number(null) is 0).
    assert.equal(at({ age_seconds: 400000 }), false);
    assert.equal(at({ age_seconds: 400000, process_age_seconds: null }), false);
    assert.equal(at({ age_seconds: null, process_age_seconds: 60000 }), false);
    assert.equal(at({ age_seconds: 'old', process_age_seconds: 60000 }), false);
    assert.equal(at({ age_seconds: Infinity, process_age_seconds: 60000 }), false);
    assert.equal(at({ age_seconds: 400000, process_age_seconds: NaN }), false);
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

test('deriveStatus: a turn delegated to a subagent is processing, not quiet', () => {
    // A session whose whole turn runs inside a subagent appends nothing of its
    // own meanwhile - no turn at all, so classify has nothing to go on and says
    // unknown. The running agent is the only evidence it is busy, and without
    // the promotion the session would sit in the quiet band while it works.
    const delegated = raw({ has_activity: false, last_entry_kind: null, subagents_running: 1, child_count: 0 });

    assert.equal(logic.classify(delegated), 'unknown');
    assert.equal(logic.deriveStatus(delegated), 'processing');
});

test('deriveStatus: a transcript with no turn at all is quiet, never "your turn"', () => {
    // Bookkeeping-only transcripts report has_activity false, so an untouched
    // session reads unknown ("Quiet") rather than claiming the user is owed a
    // reply it has no evidence for.
    const empty = raw({ has_activity: false, last_entry_kind: null, subagents_running: 0, child_count: 0 });

    assert.equal(logic.deriveStatus(empty), 'unknown');
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

test('deriveStatus: a reopened session reads idle on the strength of its process start', () => {
    // A VS Code window reopened a session four days after its last turn (a
    // user entry no reply ever followed): the process that would have replied
    // is gone, and the one holding the session now has not taken a turn.
    // Without this the dangling user turn read "working" for good.
    const reopened = raw({
        last_entry_kind: 'user_text', native_status: null, child_count: 0,
        age_seconds: 400000, process_age_seconds: 60000,
    });
    assert.equal(logic.deriveStatus(reopened), 'awaiting_input');
    // The same turn under the process that wrote it is a thinking session.
    assert.equal(logic.deriveStatus(Object.assign({}, reopened, { process_age_seconds: 500000 })), 'working');
});

test('deriveStatus: a reopened session ignores the earlier process\'s phantom subagents and workflows', () => {
    // In-process work died with the earlier process; only an OS child process
    // can survive it, and the registry stays free to say the new process is busy.
    const reopened = { last_entry_kind: 'user_text', native_status: null, age_seconds: 400000, process_age_seconds: 60000 };

    assert.equal(logic.deriveStatus(raw(Object.assign({}, reopened, { subagents_running: 1, child_count: 0 }))), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw(Object.assign({}, reopened, {
        child_count: 0, workflows: [{ run_id: 'w', total: 4, done: 1, active: true }],
    }))), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw(Object.assign({}, reopened, { child_count: 1 }))), 'processing');
    assert.equal(logic.deriveStatus(raw(Object.assign({}, reopened, { native_status: 'busy', child_count: 0 }))), 'working');
});

test('deriveStatus: a finished turn with an active workflow is processing (bridges the fan-out gap)', () => {
    // The turn ended, but the workflow is still active (its journal reports open
    // agents, or was written within the grace window). Even with no single agent
    // momentarily running, the session reads as background work, not "your turn".
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
        subagents_running: 0, child_count: 0,
        workflows: [{ run_id: 'wf_1', total: 12, done: 12, active: true }],
    }));
    assert.equal(status, 'processing');
});

test('deriveStatus: an interrupted session with a phantom active workflow stays interrupted', () => {
    // The interrupt tore down the in-process workflow; its still-"active" flag is
    // a phantom the recent window has not cleared and must not promote the session.
    const status = logic.deriveStatus(raw({
        last_entry_kind: 'user_interrupt', native_status: null,
        subagents_running: 0, child_count: 0,
        workflows: [{ run_id: 'wf_1', total: 12, done: 6, active: true }],
    }));
    assert.equal(status, 'interrupted');
});

test('activeWorkflows: keeps only active runs and tolerates a missing field', () => {
    assert.deepEqual(logic.activeWorkflows(undefined), []);
    assert.deepEqual(logic.activeWorkflows([{ active: false }, { run_id: 'w', active: true }]), [{ run_id: 'w', active: true }]);
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

test('deriveStatus: a pending command in auto-edit mode needs you right away', () => {
    // The reported case: a VS Code session in acceptEdits, a PowerShell call
    // waiting on its permission prompt, no child process, fresh entry - it read
    // as "working" until the stalled fallback caught it five minutes later.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'PowerShell', permission_mode: 'acceptEdits',
        child_count: 0, last_entry_kind: 'assistant', age_seconds: 4,
    })), 'awaiting_permission');
    // ... while the edit the mode does wave through keeps working.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Edit', permission_mode: 'acceptEdits',
        child_count: 0, last_entry_kind: 'assistant', age_seconds: 4,
    })), 'working');
});

test('pendingIsBlocking', () => {
    assert.equal(logic.pendingIsBlocking('AskUserQuestion', 'auto'), true);
    assert.equal(logic.pendingIsBlocking('ExitPlanMode', 'acceptEdits'), true);
    assert.equal(logic.pendingIsBlocking('Edit', 'default'), true);
    assert.equal(logic.pendingIsBlocking('Edit', 'auto'), false);
});

test('pendingIsBlocking: acceptEdits waives the prompt for edits only', () => {
    // Auto-edit is not auto-everything: a file edit runs unattended, but a
    // command, a fetch or an MCP call still asks. Reading the mode as a blanket
    // allow is what left a session on an open permission prompt reading
    // "working" - with nothing ever appended to correct it.
    for (const tool of ['Edit', 'MultiEdit', 'Write', 'NotebookEdit']) {
        assert.equal(logic.pendingIsBlocking(tool, 'acceptEdits'), false);
    }
    for (const tool of ['Bash', 'PowerShell', 'WebFetch', 'mcp__server__do_thing']) {
        assert.equal(logic.pendingIsBlocking(tool, 'acceptEdits'), true);
    }
    // The modes that genuinely never prompt stay untouched, edits or not.
    // `dontAsk` is one of them: it denies what would have prompted instead of
    // showing it, so nothing there ever waits on you.
    for (const mode of ['auto', 'bypassPermissions', 'plan', 'dontAsk', null, undefined]) {
        assert.equal(logic.pendingIsBlocking('Bash', mode), false);
    }
});

/* --- a pending tool that stopped going anywhere --- */

const STALE = logic.STALLED_PENDING_SECONDS + 1;
const AUTO_STALE = logic.AUTO_STALLED_PENDING_SECONDS + 1;

test('deriveStatus: a pending tool with no child and a long-still transcript stops reading as working', () => {
    // A session abandoned mid-call: the tool_use is in the transcript, no
    // tool_result ever arrived, nothing is executing, and nothing has been
    // appended since. Either a permission dialog is open or the session was
    // left - both put the ball in the user's court, so it must not claim to
    // be working (which it did indefinitely, for as long as the process lived).
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Bash', permission_mode: null,
        child_count: 0, last_entry_kind: 'assistant', age_seconds: STALE,
    })), 'awaiting_permission');
});

test('deriveStatus: a freshly pending tool is still working', () => {
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Bash', permission_mode: 'auto',
        child_count: 0, last_entry_kind: 'assistant', age_seconds: 5,
    })), 'working');
});

test('deriveStatus: a long-running tool with a live child keeps working', () => {
    // The whole point of the child gate: a build or test run that legitimately
    // takes an hour is executing, however old the last transcript entry is.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Bash', permission_mode: 'auto',
        child_count: 1, last_entry_kind: 'assistant', age_seconds: STALE * 20,
    })), 'working');
});

test('deriveStatus: a pending call with a subagent working under it is executing', () => {
    // A Task call spawns no OS child, so the child gate cannot speak for it -
    // but a running subagent (or a workflow between its fan-out phases) proves
    // the call is executing, and no mode ever prompts for it. Without this the
    // prompting modes above would report every auto-edit session with a working
    // subagent as blocked.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Task', permission_mode: 'acceptEdits',
        child_count: 0, subagents_running: 1, last_entry_kind: 'assistant', age_seconds: 90,
    })), 'working');
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Task', permission_mode: 'default',
        child_count: 0, workflows: [{ run_id: 'w', total: 4, done: 1, active: true }],
        last_entry_kind: 'assistant', age_seconds: 90,
    })), 'working');
    // A finished subagent count no longer excuses the pending call.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Task', permission_mode: 'acceptEdits',
        child_count: 0, subagents_running: 0, workflows: [{ run_id: 'w', active: false }],
        last_entry_kind: 'assistant', age_seconds: 90,
    })), 'awaiting_permission');
    // A dialog still wins over unrelated background work, as with a child.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'AskUserQuestion', permission_mode: 'auto',
        child_count: 0, subagents_running: 2, last_entry_kind: 'assistant', age_seconds: 90,
    })), 'awaiting_permission');
    // And it outlasts the stalled window, exactly as a live child does: the main
    // transcript stands still for as long as the subagent works, which is what
    // that window would otherwise read as an abandoned call.
    assert.equal(logic.deriveStatus(raw({
        pending_tool: true, last_tool_name: 'Task', permission_mode: 'auto',
        child_count: 0, subagents_running: 1, last_entry_kind: 'assistant', age_seconds: STALE * 4,
    })), 'working');
});

test('deriveStatus: a missing or mistyped age never counts as stalled', () => {
    // Infinity is the one that would otherwise pass the comparison: an age the
    // snapshot could not supply is no evidence, so it must not raise attention.
    for (const age of [undefined, null, NaN, 'soon', Infinity, -Infinity]) {
        assert.equal(logic.deriveStatus(raw({
            pending_tool: true, last_tool_name: 'Bash', permission_mode: 'auto',
            child_count: 0, last_entry_kind: 'assistant', age_seconds: age,
        })), 'working');
    }
});

test('deriveStatus: an old session with no pending tool is untouched by the rule', () => {
    // The rule is scoped to an unanswered tool_use. A finished turn stays idle
    // and a thinking session stays working, however long they have been quiet -
    // that is the structural principle the time input must not erode.
    assert.equal(logic.deriveStatus(raw({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', age_seconds: STALE * 100,
    })), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw({
        last_entry_kind: 'tool_result', age_seconds: STALE * 100,
    })), 'working');
});

test('pendingBlockReason names why the pending tool blocks', () => {
    const at = (overrides) => logic.pendingBlockReason(raw(Object.assign({ pending_tool: true, child_count: 0 }, overrides)));

    assert.equal(at({ last_tool_name: 'AskUserQuestion', permission_mode: 'auto' }), 'dialog');
    assert.equal(at({ last_tool_name: 'Edit', permission_mode: 'default' }), 'prompt');
    assert.equal(at({ last_tool_name: 'Bash', permission_mode: 'auto', age_seconds: STALE }), 'stalled');
    assert.equal(at({ last_tool_name: 'Bash', permission_mode: 'auto', age_seconds: 5 }), null);
    assert.equal(logic.pendingBlockReason(raw({ pending_tool: false, age_seconds: STALE })), null);
});

test('pendingBlockReason: auto mode gives a standing-still call a shorter window', () => {
    // Auto mode hands the approval to a classifier that answers in seconds, and
    // it still prompts in cases no mode table predicts (an ask rule, an MCP tool
    // needing interaction, the pause after repeated classifier blocks). None of
    // that is written to disk while the dialog waits, so the shorter window is
    // the only thing that catches it - five minutes on "working" was permanent,
    // since nothing is appended to correct it.
    assert.ok(logic.AUTO_STALLED_PENDING_SECONDS < logic.STALLED_PENDING_SECONDS);

    const at = (overrides) => logic.pendingBlockReason(raw(Object.assign({
        pending_tool: true, last_tool_name: 'Bash', child_count: 0,
    }, overrides)));

    assert.equal(at({ permission_mode: 'auto', age_seconds: AUTO_STALE }), 'stalled');
    // Every other non-prompting mode keeps the long window at the same age.
    for (const mode of ['plan', 'bypassPermissions', 'dontAsk']) {
        assert.equal(at({ permission_mode: mode, age_seconds: AUTO_STALE }), null);
        assert.equal(at({ permission_mode: mode, age_seconds: STALE }), 'stalled');
    }
    // Both guards survive the shorter window: a live child process means a tool
    // is genuinely running, and an unusable age is no evidence at all.
    assert.equal(at({ permission_mode: 'auto', age_seconds: AUTO_STALE, child_count: 1 }), null);
    assert.equal(at({ permission_mode: 'auto', age_seconds: AUTO_STALE, subagents_running: 1 }), null);
    assert.equal(at({ permission_mode: 'auto', age_seconds: NaN }), null);
    assert.equal(at({ permission_mode: 'auto', age_seconds: Infinity }), null);
});

test('buildSession: a stalled call stays neutral, a real prompt still names itself', () => {
    // A stalled call cannot say what it waits for, so it must not claim a
    // permission is needed; a genuine prompt or dialog keeps its specific label.
    const labels = {
        status_awaiting_permission: 'Permission needed',
        status_needs_you: 'Waiting for you',
        status_question: 'Question for you',
    };
    const build = (overrides) => logic.buildSession(raw(Object.assign({
        session_id: 's', pending_tool: true, child_count: 0, last_entry_kind: 'assistant',
    }, overrides)), labels, {});

    assert.equal(build({ last_tool_name: 'Bash', permission_mode: 'auto', age_seconds: STALE }).status_label, 'Waiting for you');
    assert.equal(build({ last_tool_name: 'Edit', permission_mode: 'default', age_seconds: 5 }).status_label, 'Permission needed');
    assert.equal(build({ last_tool_name: 'AskUserQuestion', permission_mode: 'auto', age_seconds: STALE }).status_label, 'Question for you');
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
    // An errored session shows the cause its error fields named (built by
    // apiErrorLabel below); an error nothing could name stays generic.
    assert.equal(logic.attentionLabel('errored', null, labels, 'Usage limit reached'), 'Usage limit reached');
    assert.equal(logic.attentionLabel('errored', null, labels, null), 'Error');
    // Other statuses fall through to their plain label.
    assert.equal(logic.attentionLabel('working', null, labels), 'Working');
});

test('apiErrorLabel: the cause is named from the error entry own fields', () => {
    const labels = {
        status_errored: 'Error',
        status_errored_reason: 'Error: {reason}',
        status_usage_limit: 'Usage limit reached',
        error_overloaded: 'Servers overloaded',
        error_server: 'Server-side problem',
        error_invalid_request: 'Request rejected',
        error_auth: 'Authentication failed',
        error_http: 'HTTP {status}',
    };
    const label = (raw) => logic.apiErrorLabel(raw, labels);

    // A usage limit keeps its own standalone label: it is not a fault to report
    // but a wait to sit out, and either field alone identifies it.
    assert.equal(label({ api_error_kind: 'rate_limit', api_error_status: 429 }), 'Usage limit reached');
    assert.equal(label({ api_error_kind: null, api_error_status: 429 }), 'Usage limit reached');
    assert.equal(label({ api_error_kind: 'rate_limit', api_error_status: null }), 'Usage limit reached');
    // The status is the more specific of the two fields: Claude Code writes an
    // overload as a plain server_error carrying status 529.
    assert.equal(label({ api_error_kind: 'server_error', api_error_status: 529 }), 'Error: Servers overloaded');
    // The token carries it alone when the failure has no HTTP status.
    assert.equal(label({ api_error_kind: 'overloaded_error', api_error_status: null }), 'Error: Servers overloaded');
    assert.equal(label({ api_error_kind: 'server_error', api_error_status: 500 }), 'Error: Server-side problem');
    assert.equal(label({ api_error_kind: 'invalid_request', api_error_status: null }), 'Error: Request rejected');
    assert.equal(label({ api_error_kind: 'authentication_failed', api_error_status: 401 }), 'Error: Authentication failed');
    // An unrecognized token with a 5xx is still a server-side failure...
    assert.equal(label({ api_error_kind: 'edge_proxy_error', api_error_status: 521 }), 'Error: Server-side problem');
    // ...and anything else with a status names just the status, never the raw
    // token, which would put an untranslated internal name in a localized UI.
    assert.equal(label({ api_error_kind: 'brand_new_token', api_error_status: 418 }), 'Error: HTTP 418');
    // Defensive: a status written as a string reads the same as a number.
    assert.equal(label({ api_error_kind: 'server_error', api_error_status: '529' }), 'Error: Servers overloaded');
    // Nothing identifiable: null, so the caller keeps today's plain "Error".
    assert.equal(label({ api_error_kind: 'brand_new_token', api_error_status: null }), null);
    assert.equal(label({}), null);
    assert.equal(label(null), null);
    // The token is untrusted on-disk data, so an inherited Object property must
    // never resolve as a label key.
    assert.equal(label({ api_error_kind: 'constructor' }), null);
    assert.equal(label({ api_error_kind: 'toString' }), null);
    // A label set with no wording for the cause degrades to the plain label too.
    assert.equal(logic.apiErrorLabel({ api_error_kind: 'server_error', api_error_status: 500 }, {}), null);
});

test('statusTip: the error wording rides under the label, never over it', () => {
    // The label alone is the row and header text; the tooltip adds the second
    // line, separated by the newline the tooltip renders as a line break.
    assert.equal(logic.statusTip('Error: Servers overloaded', 'API Error: 529 Overloaded.'),
        'Error: Servers overloaded\nAPI Error: 529 Overloaded.');
    // Nothing to add: the tip stays exactly the label, never a trailing newline
    // that would open a blank line in the popup.
    assert.equal(logic.statusTip('Idle', null), 'Idle');
    assert.equal(logic.statusTip('Idle', ''), 'Idle');
    assert.equal(logic.statusTip('Idle', '   \n '), 'Idle');
    assert.equal(logic.statusTip('Idle', 42), 'Idle');
    assert.equal(logic.statusTip('Idle', undefined), 'Idle');
    // A detail with no label is still worth showing on its own.
    assert.equal(logic.statusTip('', 'API Error: 500'), 'API Error: 500');
});

test('buildSession: the error wording reaches the tooltip and only the tooltip', () => {
    const labels = {
        status_errored: 'Error', status_errored_reason: 'Error: {reason}',
        error_overloaded: 'Servers overloaded', status_awaiting_input: 'Idle',
    };
    const build = (overrides) => logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence',
        native_status: null, usage: {}, child_count: 0, ...overrides,
    }, labels, {});

    const errored = build({
        api_error_kind: 'server_error', api_error_status: 529,
        api_error_detail: 'API Error: 529 Overloaded. Try again in a moment.',
    });
    // The label the row and the panel header show stays the short one...
    assert.equal(errored.status_label, 'Error: Servers overloaded');
    // ...and the dot's tooltip carries the CLI's own wording beneath it.
    assert.equal(errored.status_tip, 'Error: Servers overloaded\nAPI Error: 529 Overloaded. Try again in a moment.');

    // A detail left over on a session that is no longer errored (a lagging
    // record) must not reach the tooltip: the second line describes a stopped
    // turn, and no other status has one.
    const idle = build({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn',
        api_error_detail: 'API Error: 529 Overloaded. Try again in a moment.',
    });
    assert.equal(idle.status, 'awaiting_input');
    assert.equal(idle.status_tip, 'Idle');
});

/* --- status refinements: what the transcript and registry say beyond the status --- */

const REFINED_LABELS = {
    status_working: 'Working', status_processing: 'Background', status_awaiting_input: 'Idle',
    status_awaiting_permission: 'Permission needed', status_needs_you: 'Waiting for you', status_question: 'Question for you',
    status_errored: 'Error', status_errored_reason: 'Error: {reason}', status_usage_limit: 'Usage limit reached',
    status_sleeping: 'Wakes in {time}', status_retrying: 'Retrying ({attempt}/{max})',
    status_offline: 'Offline - retrying ({attempt}/{max})', status_blocked: 'Blocked - needs your decision',
    status_asks: 'Finished with a question', status_for: 'for {time}',
    status_limit_resets: '{limit} - resets in {time}', status_limit_over: '{limit} reset - you can continue',
    limit_five_hour: '5-hour limit', limit_seven_day: 'Weekly limit', limit_usage: 'Usage limit',
    origin_task: 'Started by a finished background task', origin_peer: 'Started by a message from another session',
    age_seconds: '{s}s', age_minutes: '{m}m', age_hours: '{h}h {m}m', age_days: '{d}d',
};

function refinedSession(overrides) {
    return logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true, has_activity: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', native_status: null, usage: {}, child_count: 0,
        ...overrides,
    }, REFINED_LABELS, {});
}

test('classify: a turn that ended blocked waits for you like an open dialog', () => {
    const ended = raw({ last_entry_kind: 'assistant', last_stop_reason: 'end_turn' });
    assert.equal(logic.classify({ ...ended, turn_end_blocked: true }), 'awaiting_permission');
    assert.equal(logic.classify(ended), 'awaiting_input');
});

test('deriveStatus: a blocked wrap-up survives a lagging registry idle', () => {
    assert.equal(logic.deriveStatus(raw({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', turn_end_signal: 'blocked', native_status: 'idle',
    })), 'awaiting_permission');
    // A question at the end of the turn is still the calm finished state.
    assert.equal(logic.deriveStatus(raw({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', turn_end_signal: 'ask',
    })), 'awaiting_input');
});

test('deriveStatus: a finished turn whose loop wakes itself is background, not your turn', () => {
    const ended = { last_entry_kind: 'assistant', last_stop_reason: 'end_turn', age_seconds: 20, process_age_seconds: 600 };
    assert.equal(logic.deriveStatus(raw({ ...ended, wakeup_in_seconds: 240 })), 'processing');
    // Due or past: the loop either woke already or no longer will.
    assert.equal(logic.deriveStatus(raw({ ...ended, wakeup_in_seconds: 0 })), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw({ ...ended, wakeup_in_seconds: -30 })), 'awaiting_input');
    // A missing or mistyped deadline is no evidence.
    assert.equal(logic.deriveStatus(raw({ ...ended, wakeup_in_seconds: null })), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw({ ...ended, wakeup_in_seconds: 'soon' })), 'awaiting_input');
});

test('deriveStatus: a wakeup never outranks you or a stop', () => {
    const ended = { last_entry_kind: 'assistant', last_stop_reason: 'end_turn', age_seconds: 20, process_age_seconds: 600, wakeup_in_seconds: 240 };
    // The turn ended on a question or a block: it waits for you whatever the loop planned.
    assert.equal(logic.deriveStatus(raw({ ...ended, turn_end_signal: 'ask' })), 'awaiting_input');
    assert.equal(logic.deriveStatus(raw({ ...ended, turn_end_signal: 'blocked' })), 'awaiting_permission');
    // You interrupted the loop.
    assert.equal(logic.deriveStatus(raw({ ...ended, last_entry_kind: 'user_interrupt' })), 'interrupted');
    // The wakeup lived in the earlier process of a reopened session.
    assert.equal(logic.deriveStatus(raw({ ...ended, age_seconds: 900 })), 'awaiting_input');
    // An ended process wakes nothing.
    assert.equal(logic.deriveStatus(raw({ ...ended, alive: false })), 'completed');
    // A turn still running is untouched.
    assert.equal(logic.deriveStatus(raw({ ...ended, last_stop_reason: 'tool_use' })), 'working');
});

test('buildSession: a sleeping loop says when it wakes, unless other work runs beside it', () => {
    const sleeping = { age_seconds: 20, process_age_seconds: 600, wakeup_in_seconds: 245 };
    assert.equal(refinedSession(sleeping).status_label, 'Wakes in 4m');
    assert.equal(refinedSession({ ...sleeping, subagents_running: 1 }).status_label, 'Background');
    assert.equal(refinedSession({ ...sleeping, child_count: 1 }).status_label, 'Background');
});

test('buildSession: a retried request says so, with the CLI summary in the tooltip only', () => {
    const retrying = { last_entry_kind: 'user_text', last_stop_reason: null, api_retry_attempt: 2, api_retry_max: 10,
        api_retry_detail: 'Connection dropped (ECONNRESET)' };
    const session = refinedSession(retrying);
    assert.equal(session.status, 'working');
    assert.equal(session.status_label, 'Retrying (2/10)');
    assert.equal(session.status_tip, 'Retrying (2/10)\nConnection dropped (ECONNRESET)');
    assert.equal(refinedSession({ ...retrying, api_retry_offline: true }).status_label, 'Offline - retrying (2/10)');
    // Unreadable counters: nothing to say beyond "Working", and no orphaned detail.
    const unreadable = refinedSession({ ...retrying, api_retry_attempt: 'two' });
    assert.equal(unreadable.status_label, 'Working');
    assert.equal(unreadable.status_tip, 'Working');
});

test('buildSession: a usage limit counts down to its reset, then says you can go on', () => {
    const limited = { last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence', api_error_kind: 'rate_limit',
        api_error_status: 429 };
    assert.equal(refinedSession({ ...limited, rate_limit_type: 'five_hour', rate_limit_resets_in_seconds: 7800 }).status_label,
        '5-hour limit - resets in 2h 10m');
    assert.equal(refinedSession({ ...limited, rate_limit_type: 'seven_day', rate_limit_resets_in_seconds: 3 * 86400 + 60 }).status_label,
        'Weekly limit - resets in 3d');
    assert.equal(refinedSession({ ...limited, rate_limit_type: 'seven_day_opus', rate_limit_resets_in_seconds: -5 }).status_label,
        'Weekly limit reset - you can continue');
    // An unknown limit type is the generic limit, an inherited property never a label key.
    assert.equal(refinedSession({ ...limited, rate_limit_type: 'brand_new', rate_limit_resets_in_seconds: 90 }).status_label,
        'Usage limit - resets in 1m');
    assert.equal(refinedSession({ ...limited, rate_limit_type: 'constructor', rate_limit_resets_in_seconds: 90 }).status_label,
        'Usage limit - resets in 1m');
    // Without a readable reset the plain label stays; the status stays errored either way.
    const plain = refinedSession({ ...limited, rate_limit_type: 'five_hour', rate_limit_resets_in_seconds: null });
    assert.equal(plain.status_label, 'Usage limit reached');
    assert.equal(plain.status, 'errored');
});

test('buildSession: how a turn ended and what the registry waits for refine the label', () => {
    assert.equal(refinedSession({ turn_end_signal: 'blocked' }).status_label, 'Blocked - needs your decision');
    assert.equal(refinedSession({ turn_end_signal: 'ask' }).status_label, 'Finished with a question');
    // Current Claude Code writes "input needed" for an open question only...
    const waiting = { last_entry_kind: 'user_text', last_stop_reason: null, native_status: 'waiting' };
    assert.equal(refinedSession({ ...waiting, waiting_for: 'input needed' }).status_label, 'Question for you');
    // ...while "permission prompt" was once written for questions too, so it stays neutral.
    assert.equal(refinedSession({ ...waiting, waiting_for: 'permission prompt' }).status_label, 'Waiting for you');
});

test('statusDetailLines: each line speaks only for the status it describes', () => {
    const lines = (status, overrides) => logic.statusDetailLines(status, raw(overrides), REFINED_LABELS);
    // How long the registry has held its status, where the two agree.
    assert.deepEqual(lines('working', { native_status: 'busy', native_status_age_seconds: 720 }), ['for 12m']);
    assert.deepEqual(lines('awaiting_input', { native_status: 'idle', native_status_age_seconds: 10200 }), ['for 2h 50m']);
    assert.deepEqual(lines('awaiting_input', { native_status: 'busy', native_status_age_seconds: 720 }), []);
    assert.deepEqual(lines('working', { native_status: 'busy', native_status_age_seconds: null }), []);
    assert.deepEqual(lines('working', { native_status: 'constructor', native_status_age_seconds: 5 }), []);
    // What opened the running turn - never for one you typed, never once it is over.
    assert.deepEqual(lines('working', { turn_origin: 'task-notification' }), ['Started by a finished background task']);
    assert.deepEqual(lines('working', { turn_origin: 'peer' }), ['Started by a message from another session']);
    assert.deepEqual(lines('working', { turn_origin: 'human' }), []);
    assert.deepEqual(lines('awaiting_input', { turn_origin: 'peer' }), []);
    // The error's own line, only while errored.
    assert.deepEqual(lines('errored', { api_error_detail: 'API Error: 500' }), ['API Error: 500']);
    assert.deepEqual(lines('awaiting_input', { api_error_detail: 'API Error: 500' }), []);
});

test('statusTip: several detail lines follow the label in order, blanks dropped', () => {
    assert.equal(logic.statusTip('Working', ['Started by a finished background task', null, '  ', 'for 12m']),
        'Working\nStarted by a finished background task\nfor 12m');
    assert.equal(logic.statusTip('Idle', []), 'Idle');
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

test('historyRange: an unknown or missing key falls back to the default window', () => {
    // A stale value from localStorage must never leave the listing unbounded.
    assert.equal(logic.historyRange('7d').key, '7d');
    assert.equal(logic.historyRange('nonsense').key, logic.DEFAULT_HISTORY_RANGE);
    assert.equal(logic.historyRange(null).key, logic.DEFAULT_HISTORY_RANGE);
    assert.equal(logic.historyRange(undefined).key, logic.DEFAULT_HISTORY_RANGE);
    assert.notEqual(logic.historyRangeSeconds(logic.DEFAULT_HISTORY_RANGE), null);
    assert.equal(logic.historyRangeSeconds('all'), null);
});

test('historyRangeCovered: only a wider window needs a re-scan', () => {
    // Narrowing is served from the cache (the fetch returned everything inside
    // the wider window); widening - and "all", which only itself covers - has to
    // go back to the backend.
    assert.equal(logic.historyRangeCovered(86400, 3600), true);
    assert.equal(logic.historyRangeCovered(86400, 86400), true);
    assert.equal(logic.historyRangeCovered(3600, 86400), false);
    assert.equal(logic.historyRangeCovered(null, 86400), true);
    assert.equal(logic.historyRangeCovered(null, null), true);
    assert.equal(logic.historyRangeCovered(86400, null), false);
    // Nothing fetched yet covers nothing.
    assert.equal(logic.historyRangeCovered(undefined, 3600), false);
    assert.equal(logic.historyRangeCovered(undefined, null), false);
});

test('filterHistoryByAge: narrows to the window, keeps records it cannot date', () => {
    const records = [
        { session_id: 'fresh', age_seconds: 600 },
        { session_id: 'edge', age_seconds: 86400 },
        { session_id: 'old', age_seconds: 5 * 86400 },
        { session_id: 'undated', age_seconds: null },
    ];

    const ids = logic.filterHistoryByAge(records, 86400).map((r) => r.session_id);
    assert.deepEqual(ids, ['fresh', 'edge', 'undated']);

    // No window keeps everything; a non-array input is an empty list, never a throw.
    assert.equal(logic.filterHistoryByAge(records, null).length, 4);
    assert.deepEqual(logic.filterHistoryByAge(null, 86400), []);
    assert.deepEqual(logic.filterHistoryByAge(undefined, null), []);
});

test('sessionMatchesSearch: only a result that arrived counts, a pending search finds nothing yet', () => {
    const set = new Set(['a']);
    assert.equal(logic.sessionMatchesSearch('a', false, null), true);       // no query
    assert.equal(logic.sessionMatchesSearch('a', true, null), false);       // query pending in debounce -> nothing found yet
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

test('searchScopeRefs: the widened scope reaches past the chips, history included', () => {
    // What the empty result's "search hidden sessions too" offer switches on:
    // the chip filter comes off the SCOPE, so a hidden live session and the past
    // sessions are read even though neither chip is on.
    const shown = { session_id: 'a', cwd: 'd:\\a', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' };
    const hidden = { session_id: 'b', cwd: 'd:\\b', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'end_turn' };
    const history = [{ session_id: 'h', cwd: 'd:\\h', alive: false, is_history: true }];
    const filters = new Set(['working']);

    const narrow = logic.searchScopeRefs([shown, hidden], history, filters, true, false).map((r) => r.session_id);
    assert.deepEqual(narrow, ['a']);

    const wide = logic.searchScopeRefs([shown, hidden], history, filters, true, true).map((r) => r.session_id).sort();
    assert.deepEqual(wide, ['a', 'b', 'h']);

    // The delta rescan keeps its own rule on top: dead sessions cannot gain a
    // match, so widening must not start re-reading them every poll.
    const delta = logic.searchScopeRefs([shown, hidden], history, filters, false, true).map((r) => r.session_id).sort();
    assert.deepEqual(delta, ['a', 'b']);
});

test('nextHistoryRange: steps one window wider, and stops at the widest', () => {
    assert.equal(logic.nextHistoryRange('1h'), '24h');
    assert.equal(logic.nextHistoryRange('24h'), '7d');
    assert.equal(logic.nextHistoryRange('30d'), 'all');
    assert.equal(logic.nextHistoryRange('all'), null);
    // An unknown key resolves through historyRange's default (24h) first.
    assert.equal(logic.nextHistoryRange('nonsense'), '7d');
});

test('searchWideningStep: offers the cheap step first, then the costly one, then nothing', () => {
    const all = new Set(['needs', 'idle', 'working', 'history']);

    // A chip is off: the sessions already in memory are the cheapest thing to add.
    assert.equal(logic.searchWideningStep({
        allKeys: all, activeKeys: new Set(['needs', 'idle', 'working']), includeHidden: false, historyRange: '24h',
    }), 'hidden');

    // Every chip on, but the window still bounds how far back the scan reaches.
    assert.equal(logic.searchWideningStep({
        allKeys: all, activeKeys: all, includeHidden: false, historyRange: '24h',
    }), 'range');

    // Already widened past the chips: the window is what is left to give.
    assert.equal(logic.searchWideningStep({
        allKeys: all, activeKeys: new Set(['working']), includeHidden: true, historyRange: '7d',
    }), 'range');

    // Everything covered - offering a widening that widens nothing would send
    // the user in a circle, so there is no button at all.
    assert.equal(logic.searchWideningStep({
        allKeys: all, activeKeys: all, includeHidden: false, historyRange: 'all',
    }), null);
    assert.equal(logic.searchWideningStep({
        allKeys: all, activeKeys: new Set(['working']), includeHidden: true, historyRange: 'all',
    }), null);

    // Arrays work like sets, and a missing options object never throws.
    assert.equal(logic.searchWideningStep({
        allKeys: ['a', 'b'], activeKeys: ['a'], includeHidden: false, historyRange: 'all',
    }), 'hidden');
    assert.equal(logic.searchWideningStep(), 'range');
});

/* --- empty-state wording and the widened-search notice --- */

/* These build markup rather than derive data, but they belong here for the same
   reason everything else does: they are pure. render.js maps UI state onto one
   plain `view` object (emptyStateView) and the wording rules live here, so which
   reason wins for an empty view, whether a widening is offered, and what the
   notice says are all checkable without a DOM. */

const EMPTY_LABELS = {
    empty_state: 'No active Claude Code agents.',
    empty_filter: 'No agents match this filter.',
    search_loading: 'Searching sessions…',
    search_no_match: 'The search text was not found in any session.',
    search_scope_hint: 'Only the sessions currently shown were searched.',
    search_widen_hidden: 'Also search hidden sessions',
    search_widen_range: 'Search further back ({range})',
    search_widened_note: 'Outside the active filters: {count}',
    search_widened_reset: 'Reset',
    search_widened_reset_tip: 'Drop the extra hits.',
    history_range_hint: 'Only older sessions inside the selected window are listed and searched.',
    history_range_7d: '7 days',
};

const ALL_CHIPS = ['needs', 'errored', 'interrupted', 'new', 'idle', 'working', 'background', 'quiet', 'history'];

// The shipped default has every chip on except History, which is exactly why a
// first empty search can always offer the "hidden" step.
function emptyView(overrides) {
    return Object.assign({
        allKeys: ALL_CHIPS,
        activeKeys: ALL_CHIPS.filter((key) => key !== 'history'),
        includeHidden: false,
        historyRange: '24h',
        searchActive: false,
        searching: false,
        quietMessage: EMPTY_LABELS.empty_filter,
        loadingNote: '',
        labels: EMPTY_LABELS,
    }, overrides || {});
}

test('emptyStateMarkup: an unsettled view never claims "not found"', () => {
    // A running scan, and a pending typing debounce (where the rows are still
    // filtered by the PREVIOUS query's matches), both say nothing yet about the
    // query being typed - so neither may show the not-found wording.
    assert.match(logic.emptyStateMarkup(emptyView({ searchActive: true, searching: true })), /Searching sessions/);

    const loading = logic.emptyStateMarkup(emptyView({ searchActive: true, loadingNote: 'Loading older sessions…' }));
    assert.match(loading, /Loading older sessions/);
    assert.doesNotMatch(loading, /was not found/);

    // Missing input degrades to an empty block instead of throwing.
    assert.equal(logic.emptyStateMarkup(), '<div class="empty"></div>');
});

test('emptyStateMarkup: a search that matched nothing never blames the filter chips', () => {
    const searched = logic.emptyStateMarkup(emptyView({ searchActive: true }));
    assert.match(searched, /The search text was not found in any session\./);
    assert.doesNotMatch(searched, /No agents match this filter\./);

    // Without a query the filter wording is the right one, and nothing is offered.
    const filtered = logic.emptyStateMarkup(emptyView());
    assert.match(filtered, /No agents match this filter\./);
    assert.doesNotMatch(filtered, /empty-action/);
});

test('emptyStateMarkup: the widening offer escalates one step per click', () => {
    // Step 1 - the History chip is off by default, so sessions are hidden, and
    // those are the cheapest thing to add (they are already in memory).
    const hidden = logic.emptyStateMarkup(emptyView({ searchActive: true }));
    assert.match(hidden, /data-widen="hidden"/);
    assert.match(hidden, /Also search hidden sessions/);
    assert.match(hidden, /Only the sessions currently shown were searched\./);

    // Step 2 - nothing is hidden any more, but the window still bounds the scan.
    // The button names the window it would move to, so the cost of the click is
    // on the button rather than behind it.
    const range = logic.emptyStateMarkup(emptyView({ searchActive: true, includeHidden: true }));
    assert.match(range, /data-widen="range"/);
    assert.match(range, /Search further back \(7 days\)/);

    // Every chip on but the window still bounded: also the range step, never a
    // "hidden" offer that would widen nothing.
    assert.match(
        logic.emptyStateMarkup(emptyView({ searchActive: true, activeKeys: ALL_CHIPS })),
        /data-widen="range"/
    );

    // Nothing left to reach - offering a widening that widens nothing would send
    // the user in a circle, so the plain sentence is the whole answer.
    const done = logic.emptyStateMarkup(emptyView({ searchActive: true, includeHidden: true, historyRange: 'all' }));
    assert.match(done, /The search text was not found/);
    assert.doesNotMatch(done, /empty-action/);
    assert.doesNotMatch(done, /empty-hint/);
});

test('emptyStateMarkup: the history window is named only while it bounds the view', () => {
    // Chip on and a bounded window: an empty view can mean "outside the window"
    // rather than "gone", so the window has to be named.
    assert.match(logic.emptyStateMarkup(emptyView({ activeKeys: ALL_CHIPS })), /empty-hint/);

    // Chip off, or no bound at all - the window explains nothing either way.
    assert.doesNotMatch(logic.emptyStateMarkup(emptyView()), /empty-hint/);
    assert.doesNotMatch(logic.emptyStateMarkup(emptyView({ activeKeys: ALL_CHIPS, historyRange: 'all' })), /empty-hint/);
});

test('widenedNoticeMarkup: a plural-invariant count, plus the way back', () => {
    const one = logic.widenedNoticeMarkup(1, EMPTY_LABELS);
    // The label carries no counted noun and the number comes last, so a single
    // hit cannot read "1 hits" - the same idiom as every other counted label.
    assert.match(one, /Outside the active filters: 1/);
    assert.match(logic.widenedNoticeMarkup(2, EMPTY_LABELS), /Outside the active filters: 2/);

    // The reset is the only way out of the widening that keeps the query, so the
    // notice must carry it - and explain itself through the app's own tooltip,
    // never the native title attribute.
    assert.match(one, /class="notice-reset"/);
    assert.match(one, />Reset<\/button>/);
    assert.match(one, /data-tip="Drop the extra hits\."/);

    // Missing labels must still produce the box, not throw.
    assert.match(logic.widenedNoticeMarkup(1), /class="search-notice"/);
});

const HIT_LABELS = {
    search_hits_count: 'Found: {count}',
    search_hits_more: 'Not shown: {count}',
    search_hit_user: 'You',
    search_hit_thinking: 'Thinking',
    search_hit_tool_result: 'Result',
};

test('searchHitsMarkup: the count leads, the excerpts follow, the remainder is named', () => {
    const html = logic.searchHitsMarkup({
        session_id: 'a',
        count: 4,
        snippets: [
            { kind: 'user', before: 'left ', match: 'needle', after: ' right' },
            { kind: 'thinking', before: 'cut off', match: 'needle', after: '', clipped_before: true },
        ],
    }, HIT_LABELS);

    assert.match(html, /Found: 4/);
    assert.match(html, /<mark>needle<\/mark>/);
    assert.match(html, />You</);
    assert.match(html, />Thinking</);
    // Two of four hits are shown, so the rest is stated rather than left to be
    // inferred from the two numbers not matching.
    assert.match(html, /Not shown: 2/);
    // The clip marker sits on the side text was dropped from, and only there.
    assert.match(html, /…cut off<mark>/);
});

test('searchHitsMarkup: both counts are plural-invariant', () => {
    // The label carries no counted noun and the number comes last, so a single
    // hit cannot read "1 hits" - the same idiom as every other counted label.
    const one = logic.searchHitsMarkup({ count: 1, snippets: [{ kind: 'user', match: 'x' }] }, HIT_LABELS);
    assert.match(one, /Found: 1/);

    const many = logic.searchHitsMarkup({ count: 9, snippets: [{ kind: 'user', match: 'x' }] }, HIT_LABELS);
    assert.match(many, /Found: 9/);
    assert.match(many, /Not shown: 8/);
});

test('searchHitsMarkup: a count the backend stopped short of is marked, not rounded', () => {
    // The scan gives up counting past its cap, so the figure is a floor. Saying
    // "500" flat would claim a total it never established.
    const html = logic.searchHitsMarkup({
        count: 500, partial: true, snippets: [{ kind: 'user', match: 'x' }],
    }, HIT_LABELS);

    assert.match(html, /Found: 500\+/);
    assert.match(html, /Not shown: 499\+/);
});

test('searchHitsMarkup: a tool call names the tool, an unknown kind names nothing', () => {
    const html = logic.searchHitsMarkup({
        count: 2,
        snippets: [
            { kind: 'tool_use', tool: 'Bash', before: '', match: 'grep', after: '' },
            { kind: 'not-a-kind', before: '', match: 'grep', after: '' },
        ],
    }, HIT_LABELS);

    // "Bash" says far more than "Tool" would.
    assert.match(html, />Bash</);
    // An unknown kind still renders its excerpt - just without a source label.
    assert.equal((html.match(/<mark>grep<\/mark>/g) || []).length, 2);
    assert.equal((html.match(/hit-source/g) || []).length, 1);
});

test('searchHitsMarkup: nothing to report yields no markup at all', () => {
    // An empty string keeps the row's hits container out of the grid flow, so a
    // row with no hits pays neither the gap nor an empty line.
    assert.equal(logic.searchHitsMarkup(null, HIT_LABELS), '');
    assert.equal(logic.searchHitsMarkup({ count: 0 }, HIT_LABELS), '');
    assert.equal(logic.searchHitsMarkup({ count: 'lots' }, HIT_LABELS), '');

    // Every hit shown: no remainder line, which would read as hidden hits.
    const complete = logic.searchHitsMarkup({ count: 1, snippets: [{ kind: 'user', match: 'x' }] }, HIT_LABELS);
    assert.ok(!complete.includes('hit-more'));

    // Missing labels must still produce the block, not throw.
    assert.match(logic.searchHitsMarkup({ count: 1, snippets: [] }), /class="row-hits"/);
});

test('searchScopeRefs: refs carry the session origin, defaulting to windows', () => {
    // The scoped refs are what the origin-aware start_search bridge call
    // receives, so each ref needs to know which root to search.
    const winSession = { session_id: 'a', cwd: 'd:\\a', alive: true, has_transcript: true, has_activity: true, last_entry_kind: 'user_text' };
    const wslSession = {
        session_id: 'b', cwd: '/home/dev/b', alive: true, has_transcript: true, has_activity: true,
        last_entry_kind: 'user_text', origin: 'wsl:Ubuntu',
    };
    const refs = logic.searchScopeRefs([winSession, wslSession], null, new Set(['working']), true);
    assert.equal(refs.find((r) => r.session_id === 'a').origin, 'local');
    assert.equal(refs.find((r) => r.session_id === 'b').origin, 'wsl:Ubuntu');
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

test('autoModePaused reads a paused auto mode off the block streak', () => {
    // Claude Code stops deferring to the classifier after this many blocks in a
    // row and prompts for everything again - the state that makes a session look
    // misconfigured while its mode chip still reads "Auto".
    const at = (overrides) => logic.autoModePaused(raw(overrides));

    assert.equal(at({ auto_denials_consecutive: logic.AUTO_PAUSE_STREAK }), true);
    assert.equal(at({ auto_denials_consecutive: logic.AUTO_PAUSE_STREAK - 1 }), false);
    assert.equal(at({}), false);
    // An ended session's last streak describes nothing that is still true.
    assert.equal(at({ alive: false, auto_denials_consecutive: logic.AUTO_PAUSE_STREAK }), false);
    // A missing or unusable count must never read as paused; a numeric string
    // is a usable one and reads as the number it spells.
    assert.equal(at({ auto_denials_consecutive: null }), false);
    assert.equal(at({ auto_denials_consecutive: 'lots' }), false);
    assert.equal(at({ auto_denials_consecutive: NaN }), false);
    assert.equal(at({ auto_denials_consecutive: Infinity }), false);
    assert.equal(at({ auto_denials_consecutive: String(logic.AUTO_PAUSE_STREAK) }), true);
    // The counter lives in the process that ran the streak up; a reopened
    // session's new process starts at zero.
    assert.equal(at({ auto_denials_consecutive: logic.AUTO_PAUSE_STREAK, age_seconds: 400000, process_age_seconds: 60000 }), false);
});

test('buildSession carries the paused auto mode and the block total', () => {
    const build = (overrides) => logic.buildSession(raw(Object.assign({ session_id: 's' }, overrides)), {}, {});

    const paused = build({ auto_denials_consecutive: 4, auto_denials_total: 9 });
    assert.equal(paused.auto_paused, true);
    assert.equal(paused.auto_denials_total, 9);

    const running = build({ auto_denials_consecutive: 0, auto_denials_total: 9 });
    assert.equal(running.auto_paused, false);
    // The total keeps standing on its own: the streak ends with the next allowed
    // call, but what the session spent on blocked ones does not.
    assert.equal(running.auto_denials_total, 9);
    assert.equal(build({}).auto_denials_total, 0);
    // The total is interpolated into a label, so a number is what has to arrive.
    assert.equal(build({ auto_denials_total: 'many' }).auto_denials_total, 0);
    assert.equal(build({ auto_denials_total: '9' }).auto_denials_total, 9);
});

test('buildSession marks Remote Control only while the process holds the connection', () => {
    const build = (overrides) => logic.buildSession(raw(Object.assign({ session_id: 's' }, overrides)), {}, {});

    assert.equal(build({ remote_control: true }).remote_control, true);
    assert.equal(build({ remote_control: false }).remote_control, false);
    assert.equal(build({}).remote_control, false);
    // The connection lives in the session's process: an ended session's registry
    // record still names the bridge, but nothing is reachable any more.
    assert.equal(build({ remote_control: true, alive: false }).remote_control, false);
    // Only a real boolean counts - a truthy stand-in is not the backend's flag.
    assert.equal(build({ remote_control: 'session_01abc' }).remote_control, false);
    // A past session is never reachable.
    assert.equal(build({ remote_control: true, is_history: true, alive: false }).remote_control, false);
});

test('buildSession names the worktree a session works in, and only then', () => {
    const build = (overrides) => logic.buildSession(raw(Object.assign({ session_id: 's', cwd: 'd:\\WebDev\\app' }, overrides)), {}, {});

    const inWorktree = build({ worktree_cwd: 'd:\\WebDev\\app-worktrees\\feature\\', worktree_branch: 'wt/feature' });
    // The project stays the launch directory - that is what groups the row.
    assert.equal(inWorktree.cwd, 'd:\\WebDev\\app');
    assert.deepEqual(inWorktree.worktree, { name: 'feature', path: 'D:\\WebDev\\app-worktrees\\feature\\', branch: 'wt/feature' });

    assert.equal(build({ worktree_cwd: 'D:\\x\\feature', worktree_branch: null }).worktree.branch, '');
    assert.equal(build({}).worktree, null);
    assert.equal(build({ worktree_cwd: null }).worktree, null);
    assert.equal(build({ worktree_cwd: 42 }).worktree, null);
});

test('a worktree session is grouped under the project it was launched in', () => {
    const projects = logic.groupProjects([
        raw({ session_id: 'a', cwd: 'd:\\WebDev\\app' }),
        raw({ session_id: 'b', cwd: 'D:\\WebDev\\app', worktree_cwd: 'D:\\WebDev\\app-worktrees\\feature' }),
    ], {}, {});

    assert.equal(projects.length, 1);
    assert.equal(projects[0].sessions.length, 2);
});

test('a background session is told apart from a terminal one', () => {
    // `claude --bg` starts from a terminal that then goes away: the entrypoint
    // still reads `cli` while no terminal and no window remain. Marking it "CLI"
    // would promise a window to raise, and the click handler would go looking.
    const build = (overrides) => logic.buildSession(raw(Object.assign({ session_id: 's' }, overrides)), {}, {});

    const background = build({ kind: 'bg', entrypoint: 'cli', job_id: '95dc95aa' });
    assert.equal(background.background, true);
    assert.equal(background.via_cli, false);
    assert.equal(background.job_id, '95dc95aa');

    // A real terminal session keeps both.
    const terminal = build({ kind: 'interactive', entrypoint: 'cli' });
    assert.equal(terminal.background, false);
    assert.equal(terminal.via_cli, true);
    assert.equal(terminal.job_id, '');
    // A mistyped handle must never reach the copied command.
    assert.equal(build({ kind: 'bg', job_id: 17 }).job_id, '');
});

test('the on-disk label lookups cannot answer with an inherited value', () => {
    // The permission mode and the entrypoint are untrusted values off disk, and
    // an object lookup resolves 'constructor' or 'toString' to a function, which
    // would then be rendered into the mode chip or the host cell.
    for (const key of ['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty']) {
        assert.equal(typeof logic.modeLabel(key), 'string');
        assert.equal(logic.modeLabel(key), key);
        assert.equal(logic.hostLabel(null, key), null);
        assert.equal(logic.entrypointLabel(key), key);
        assert.equal(logic.kindLabelKey(key), null);
    }
    // The real values still resolve.
    assert.equal(logic.modeLabel('auto'), 'Auto');
    assert.equal(logic.hostLabel(null, 'claude-vscode'), 'VS Code');
});

test('kindLabelKey names the kinds and leaves an unknown one alone', () => {
    // The registry writes `bg`, not `background` - the mismatch that put a bare
    // "bg" in the host column while the label for it went unused.
    assert.equal(logic.kindLabelKey('bg'), 'kind_background');
    assert.equal(logic.kindLabelKey('interactive'), 'kind_interactive');
    assert.equal(logic.kindLabelKey('something-new'), null);
    // The map must not answer for anything inherited from Object.prototype.
    assert.equal(logic.kindLabelKey('constructor'), null);
    assert.equal(logic.kindLabelKey('toString'), null);
});

test('modeLabel', () => {
    assert.equal(logic.modeLabel('default'), 'Manual');
    assert.equal(logic.modeLabel('acceptEdits'), 'Auto-edit');
    assert.equal(logic.modeLabel('dontAsk'), "Don't ask");
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

test('sessionCostUsd ignores inherited Object.prototype keys', () => {
    // A model id normalizing to an inherited property name must fall back to null,
    // not resolve to a prototype member and silently price the model at $0.
    const table = { 'opus-4-8': { input: 5, output: 25, cache_read: 0, cache_write_5m: 0, cache_write_1h: 0 } };
    assert.equal(logic.sessionCostUsd({ 'claude-constructor': { output_tokens: 1000 } }, table), null);
    assert.equal(logic.sessionCostUsd({ 'claude-hasOwnProperty': { output_tokens: 1000 } }, table), null);
    assert.equal(logic.sessionCostUsd({ 'claude-opus-4-8': { output_tokens: 1000000 } }, table), 25);
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

test('buildSession: usage still being scanned reads as an ellipsis, never as a zero cost', () => {
    // A large transcript's first scan runs in the background; until it lands
    // the usage is unknown, and "<$1" or "0" would be a wrong figure.
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', model_id: 'claude-opus-4-8',
        usage: {}, usage_by_model: {}, scan_pending: true,
    }, { token_summary: '{input} in · {output} out' }, PRICES);
    assert.equal(session.usage_compact, '…');
    assert.equal(session.usage_detail, '');
    assert.equal(session.status, 'awaiting_input');
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

test('buildSession: a reopened session reads idle and clears the phantom subagent and workflow badges', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true, has_activity: true,
        last_entry_kind: 'user_text', native_status: null, usage: {},
        age_seconds: 400000, process_age_seconds: 60000,
        subagents_running: 1, subagents_labels: ['general-purpose'], child_count: 0,
        workflows: [{ run_id: 'w', total: 4, done: 1, active: true }],
    }, {}, {});
    assert.equal(session.status, 'awaiting_input');
    assert.equal(session.subagents_running, 0);
    assert.deepEqual(session.subagents_labels, []);
    assert.equal(session.workflow_total, 0);
});

test('buildSession: a usage limit reads as errored, is named specifically, and clears the phantom subagent badge', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence',
        api_error_kind: 'rate_limit', api_error_status: 429,
        native_status: null, usage: {}, subagents_running: 1, subagents_labels: ['general-purpose'], child_count: 0,
    }, { status_errored: 'Error', status_usage_limit: 'Usage limit reached' }, {});
    assert.equal(session.status, 'errored');
    assert.equal(session.status_label, 'Usage limit reached');
    assert.equal(session.needs_attention, true);
    assert.equal(session.subagents_running, 0);
    assert.deepEqual(session.subagents_labels, []);
});

test('buildSession: an active workflow exposes its total and reads as processing', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage: {},
        subagents_running: 0, subagents_labels: [], child_count: 0,
        workflows: [{ run_id: 'wf_1', total: 12, done: 4, active: true }],
    }, {}, {});
    assert.equal(session.status, 'processing');
    assert.equal(session.workflow_active, true);
    assert.equal(session.workflow_total, 12);
    assert.equal(session.workflow_done, 4);
});

test('buildSession: a force-stop clears the phantom workflow badge', () => {
    const session = logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'user_interrupt', native_status: null, usage: {},
        subagents_running: 0, subagents_labels: [], child_count: 0,
        workflows: [{ run_id: 'wf_1', total: 12, done: 4, active: true }],
    }, { status_interrupted: 'Interrupted' }, {});
    assert.equal(session.status, 'interrupted');
    assert.equal(session.workflow_active, false);
    assert.equal(session.workflow_total, 0);
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

test('buildSession: a non-limit API error reads as errored and names its cause', () => {
    const labels = {
        status_errored: 'Error', status_usage_limit: 'Usage limit reached',
        status_errored_reason: 'Error: {reason}', error_overloaded: 'Servers overloaded',
    };
    const build = (overrides) => logic.buildSession({
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'api_error', last_stop_reason: 'stop_sequence',
        native_status: null, usage: {}, child_count: 0, ...overrides,
    }, labels, {});

    const overloaded = build({ api_error_kind: 'server_error', api_error_status: 529 });
    assert.equal(overloaded.status, 'errored');
    assert.equal(overloaded.status_label, 'Error: Servers overloaded');
    // An error whose fields name nothing keeps the plain label rather than
    // inventing a cause.
    const unnamed = build({ api_error_kind: null, api_error_status: null });
    assert.equal(unnamed.status, 'errored');
    assert.equal(unnamed.status_label, 'Error');
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

/* --- WSL origin (host, flags) ---

   A raw record's origin ('local' | 'wsl:<distro>') shapes the display host
   and the wsl/origin_label flags buildSession exposes. Fixture shared across
   this group (mirrors the file's existing pattern of a base raw object spread
   with per-test overrides, e.g. the compact-cost test above); wslLabels is
   empty because none of these assertions read a formatted label string. */
const wslBaseRaw = raw({
    session_id: 's', pid: 1, cwd: '/home/dev/proj', short_name: 's',
    last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage: {}, child_count: 0,
});
const wslLabels = {};

test('buildSession: WSL origin shapes host and flags', () => {
    const session = logic.buildSession({ ...wslBaseRaw, origin: 'wsl:Ubuntu', origin_label: 'Ubuntu', entrypoint: 'cli' }, wslLabels, null);
    assert.equal(session.origin, 'wsl:Ubuntu');
    assert.equal(session.wsl, true);
    assert.equal(session.host, 'Ubuntu (WSL)');
    assert.equal(session.via_cli, true);
});

test('buildSession: absent origin defaults to windows', () => {
    const session = logic.buildSession(wslBaseRaw, wslLabels, null);
    assert.equal(session.origin, 'local');
    assert.equal(session.wsl, false);
});

test('buildSession: a WSL session with no resolved distro label hosts as plain "WSL"', () => {
    // origin_label null (the backend could not resolve a distro name): the host
    // must read the bare "WSL", never the redundant "WSL (WSL)".
    const session = logic.buildSession({ ...wslBaseRaw, origin: 'wsl:Ubuntu', origin_label: null }, wslLabels, null);
    assert.equal(session.origin_label, null);
    assert.equal(session.wsl, true);
    assert.equal(session.host, 'WSL');
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

test('cliHistory keeps the backend order, names where each run was written from, and drops unusable entries', () => {
    // A terminal and a VS Code window holding the same session: the origin is
    // what makes the version flipping between the two legible.
    const timeline = [
        { time: '2026-07-11T09:00:00Z', version: '2.1.224', entrypoint: 'claude-vscode' },
        { time: '2026-07-11T13:00:00Z', version: '2.1.228', entrypoint: 'cli' },
        { time: '2026-07-11T14:00:00Z', version: '2.1.224', entrypoint: 'claude-vscode' },
        { time: '2026-07-11T15:00:00Z', version: '2.1.228' },
    ];
    assert.deepEqual(logic.cliHistory(timeline), [
        { time: '2026-07-11T09:00:00Z', version: '2.1.224', origin: 'VS Code' },
        { time: '2026-07-11T13:00:00Z', version: '2.1.228', origin: 'CLI' },
        { time: '2026-07-11T14:00:00Z', version: '2.1.224', origin: 'VS Code' },
        { time: '2026-07-11T15:00:00Z', version: '2.1.228', origin: null },
    ]);
    // A renamed or mistyped field must not reach the renderer as an entry.
    assert.deepEqual(logic.cliHistory([{ time: 'x', version: 7 }, { time: 'y' }, null]), []);
    assert.deepEqual(logic.cliHistory([]), []);
    assert.deepEqual(logic.cliHistory(null), []);
});

test('hostLabel names a Claude Desktop session by its entrypoint', () => {
    // The desktop app's process is no editor or terminal the probe knows, so
    // the entrypoint is the only thing that names the host.
    assert.equal(logic.hostLabel(null, 'claude-desktop'), 'Claude Desktop');
    // A detected host still wins over the entrypoint.
    assert.equal(logic.hostLabel('Windows Terminal', 'claude-desktop'), 'Windows Terminal');
});

test('entrypointLabel names the known origins and falls back to the token', () => {
    assert.equal(logic.entrypointLabel('claude-vscode'), 'VS Code');
    assert.equal(logic.entrypointLabel('claude-desktop'), 'Claude Desktop');
    assert.equal(logic.entrypointLabel('cli'), 'CLI');
    // A token this build does not know is still a name of sorts.
    assert.equal(logic.entrypointLabel('sdk-ts'), 'sdk-ts');
    // Nothing usable names nothing.
    assert.equal(logic.entrypointLabel(''), null);
    assert.equal(logic.entrypointLabel(7), null);
    assert.equal(logic.entrypointLabel(undefined), null);
});

test('parseVersion and compareVersions order releases numerically', () => {
    assert.deepEqual(logic.parseVersion('2.1.318'), [2, 1, 318]);
    for (const value of [null, undefined, '', '2', '2.1.318-beta', 'v2.1.318', '2..1', 2.1, ['2.1.318']]) {
        assert.equal(logic.parseVersion(value), null, String(value));
    }
    // Numeric, not textual: 318 is newer than 99.
    assert.ok(logic.compareVersions([2, 1, 318], [2, 1, 99]) > 0);
    assert.ok(logic.compareVersions([2, 0, 999], [2, 1]) < 0);
    assert.equal(logic.compareVersions([2, 1], [2, 1, 0]), 0);
});

test('cliUpdateFor names the newer Claude Code a restart would bring', () => {
    const session = (overrides) => raw({ entrypoint: 'claude-vscode', host: 'VS Code', process_version: '2.1.318', ...overrides });
    const updateOf = (target, others) => logic.cliUpdateFor(target, logic.newestCliVersions([target, ...others]));

    // A sibling of the same app already runs a newer version.
    assert.deepEqual(updateOf(session({ process_version: '2.1.317' }), [session()]), { running: '2.1.317', available: '2.1.318' });
    // The newest one, and one on the same version, have nothing to gain.
    assert.equal(updateOf(session(), [session({ process_version: '2.1.317' })]), null);
    assert.equal(updateOf(session(), [session()]), null);
    // Numeric comparison: 2.1.99 is the older one.
    assert.deepEqual(updateOf(session({ process_version: '2.1.99' }), [session()]).available, '2.1.318');

    // Another app, another editor or another root brings its own copy and says nothing.
    const older = session({ process_version: '2.1.317' });
    assert.equal(updateOf(older, [session({ entrypoint: 'claude-desktop', host: null })]), null);
    assert.equal(updateOf(older, [session({ host: 'Cursor' })]), null);
    assert.equal(updateOf(older, [session({ origin: 'wsl:Ubuntu' })]), null);
    // An ended session runs nothing - neither as the reference nor as the target.
    assert.equal(updateOf(older, [session({ alive: false })]), null);
    assert.equal(updateOf(session({ process_version: '2.1.317', alive: false }), [session()]), null);
    // Without an app or a readable version there is nothing to compare.
    assert.equal(updateOf(session({ entrypoint: null, process_version: '2.1.317' }), [session({ entrypoint: null })]), null);
    assert.equal(updateOf(session({ process_version: 'dev' }), [session()]), null);
    assert.equal(updateOf(older, [session({ process_version: '2.1.999-beta' })]), null);
});

test('cliUpdateFor counts the installed version for the CLI only', () => {
    // The CLI runs the machine's install, so its self-update is what a restart brings -
    // even with no newer sibling running. Every terminal shares that install.
    const cli = raw({ entrypoint: 'cli', host: 'Windows Terminal', process_version: '2.1.317', installed_version: '2.1.318' });
    assert.deepEqual(logic.cliUpdateFor(cli, logic.newestCliVersions([cli])), { running: '2.1.317', available: '2.1.318' });
    const sibling = raw({ entrypoint: 'cli', host: 'PowerShell', process_version: '2.1.318' });
    const stale = { ...cli, installed_version: null };
    assert.equal(logic.cliUpdateFor(stale, logic.newestCliVersions([stale, sibling])).available, '2.1.318');

    // VS Code brings its own copy: the machine's install does not apply to it.
    const vscode = raw({ entrypoint: 'claude-vscode', host: 'VS Code', process_version: '2.1.317', installed_version: '2.1.318' });
    assert.equal(logic.cliUpdateFor(vscode, logic.newestCliVersions([vscode])), null);
    // An install older than the running process is no update.
    const ahead = raw({ entrypoint: 'cli', process_version: '2.1.318', installed_version: '2.1.294' });
    assert.equal(logic.cliUpdateFor(ahead, logic.newestCliVersions([ahead])), null);
});

test('groupProjects marks each session a restart would update', () => {
    const projects = logic.groupProjects([
        raw({ session_id: 'old', cwd: 'd:\\a', entrypoint: 'cli', process_version: '2.1.317' }),
        raw({ session_id: 'new', cwd: 'd:\\b', entrypoint: 'cli', process_version: '2.1.318' }),
        raw({ session_id: 'past', cwd: 'd:\\b', alive: false, is_history: true }),
    ], {});
    const updates = new Map(projects.flatMap((project) => project.sessions).map((session) => [session.session_id, session.cli_update]));

    // Compared across panels: the sibling that runs the newer version sits in another project.
    assert.deepEqual(updates.get('old'), { running: '2.1.317', available: '2.1.318' });
    assert.equal(updates.get('new'), null);
    assert.equal(updates.get('past'), null);
});

test('buildSession carries the CLI version and its switch history', () => {
    const base = {
        session_id: 's', pid: 1, cwd: 'd:\\x', short_name: 's', alive: true, has_transcript: true,
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', usage: {}, model_id: 'claude-opus-4-8',
    };
    const upgraded = logic.buildSession({ ...base,
        cli_version: '2.1.228',
        cli_timeline: [
            { time: '2026-07-11T09:00:00Z', version: '2.1.224' },
            { time: '2026-07-11T15:00:00Z', version: '2.1.228' },
        ],
    }, {}, {});
    assert.equal(upgraded.cli_version, '2.1.228');
    assert.equal(upgraded.cli_switched, true);
    assert.deepEqual(upgraded.cli_history.map((e) => e.version), ['2.1.224', '2.1.228']);

    // A history record has a version from its tail but no timeline to go with it,
    // which must read as "not switched", not as a switch to nothing.
    const past = logic.buildSession({ ...base, cli_version: '2.1.207', cli_timeline: [] }, {}, {});
    assert.equal(past.cli_version, '2.1.207');
    assert.equal(past.cli_switched, false);
    assert.deepEqual(past.cli_history, []);

    // A backend that reports no version at all leaves an empty string, never
    // "undefined" rendered into the column.
    assert.equal(logic.buildSession(base, {}, {}).cli_version, '');
});

test('grouping helpers', () => {
    assert.equal(logic.groupKey('d:\\WebDev\\proj'), logic.groupKey('D:\\WebDev\\proj'));
    assert.equal(logic.displayCwd('d:\\WebDev\\proj'), 'D:\\WebDev\\proj');
    assert.equal(logic.projectName('D:\\WebDev\\oku3d-app'), 'oku3d-app');
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
    assert.equal(projects[0].key, 'local|d:\\webdev\\proj');
    assert.equal(projects[0].origin_display, '');
});

test('groupProjects keeps two distros with the identical POSIX cwd apart', () => {
    // The same /home path in two distributions names two different folders:
    // they must not share a panel, its collapse state, or its open-folder
    // target - and the header carries the distro so the two stay tellable.
    const projects = logic.groupProjects([
        raw({ cwd: '/home/dev/x', session_id: 'a', origin: 'wsl:Ubuntu', origin_label: 'Ubuntu' }),
        raw({ cwd: '/home/dev/x', session_id: 'b', origin: 'wsl:Debian', origin_label: 'Debian' }),
        raw({ cwd: '/home/dev/x', session_id: 'c', origin: 'wsl:Ubuntu', origin_label: 'Ubuntu' }),
    ], {});
    assert.equal(projects.length, 2);
    const byOrigin = new Map(projects.map((p) => [p.origin, p]));
    assert.equal(byOrigin.get('wsl:Ubuntu').sessions.length, 2);
    assert.equal(byOrigin.get('wsl:Ubuntu').origin_display, 'Ubuntu (WSL)');
    assert.equal(byOrigin.get('wsl:Debian').origin_display, 'Debian (WSL)');
    assert.notEqual(byOrigin.get('wsl:Ubuntu').key, byOrigin.get('wsl:Debian').key);
});

test('scratchWorkspaceRoot recognizes only a Claude Desktop scratch folder', () => {
    const root = 'C:\\Users\\dev\\AppData\\Local\\Claude-Data\\claude_pzs8sxrjxfjjc\\scratch-workspaces';
    const cwd = root + '\\39474e52-271c-4fd5-86cd-c2ec92b0482a\\f1becc0d-292d-4dca-934f-96121b41f016\\scratch-2026-11-03-629f10';
    assert.equal(logic.scratchWorkspaceRoot(cwd), root);
    assert.equal(logic.scratchWorkspaceRoot(cwd + '\\'), root);
    assert.equal(logic.scratchWorkspaceRoot('/Users/dev/Claude-Data/claude_x/scratch-workspaces/a/b/scratch-2026-11-03-c27e42'),
        '/Users/dev/Claude-Data/claude_x/scratch-workspaces');
    // A folder of the user's own that merely looks similar keeps its name.
    assert.equal(logic.scratchWorkspaceRoot('D:\\scratch-workspaces\\scratch-2026-11-03-629f10'), null);
    assert.equal(logic.scratchWorkspaceRoot(root + '\\a\\b\\my-project'), null);
    assert.equal(logic.scratchWorkspaceRoot('D:\\WebDev\\proj'), null);
    assert.equal(logic.scratchWorkspaceRoot(null), null);
    assert.equal(logic.scratchWorkspaceRoot(42), null);
});

test('groupProjects puts every Claude Desktop scratch chat into one "This computer" panel', () => {
    const root = 'c:\\Users\\dev\\AppData\\Local\\Claude-Data\\claude_pzs8sxrjxfjjc\\scratch-workspaces';
    const projects = logic.groupProjects([
        raw({ cwd: root + '\\a1\\b1\\scratch-2026-11-03-629f10', session_id: 'a', entrypoint: 'claude-desktop' }),
        raw({ cwd: root + '\\a1\\b2\\scratch-2026-11-03-c27e42', session_id: 'b', entrypoint: 'claude-desktop' }),
        raw({ cwd: 'D:\\WebDev\\proj', session_id: 'c' }),
    ], { project_this_computer: 'Dieser Computer' });
    assert.equal(projects.length, 2);
    const scratch = projects.find((p) => p.name === 'Dieser Computer');
    assert.equal(scratch.sessions.length, 2);
    // The header opens the shared folder; each row keeps its own cwd.
    assert.equal(scratch.cwd, 'C' + root.slice(1));
    assert.equal(scratch.sessions[0].cwd, root + '\\a1\\b1\\scratch-2026-11-03-629f10');
    assert.equal(projects.find((p) => p !== scratch).name, 'proj');
    // Without a translation the English name stands in.
    assert.equal(logic.groupProjects([raw({ cwd: root + '\\a\\b\\scratch-2026-11-03-629f10' })], {})[0].name, 'This computer');
});

function project(name, statuses, ages) {
    const sessions = statuses.map((status, index) => ({ status, age_seconds: ages ? ages[index] : null }));
    return { name, cwd: 'd:\\repos\\' + name, sessions };
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

test('sortProjects: priority order groups by band, then alphabetically when no age is known', () => {
    const projects = [
        project('zeta', ['working']),
        project('alpha', ['completed']),
        project('beta', ['awaiting_input']),
        project('gamma', ['awaiting_permission']),
    ];
    const names = logic.sortProjects(projects, true).map((p) => p.name);
    // band 0 (gamma, blocked), then band 1 (zeta, working), then band 2 (alpha,
    // beta - finished/idle); with no age to compare, the name breaks the tie.
    assert.deepEqual(names, ['gamma', 'zeta', 'alpha', 'beta']);
});

test('projectActivityAge: the freshest session wins, an unknown age never does', () => {
    assert.equal(logic.projectActivityAge([{ age_seconds: 900 }, { age_seconds: 60 }]), 60);
    assert.equal(logic.projectActivityAge([{ age_seconds: null }, { age_seconds: 42 }]), 42);
    assert.equal(logic.projectActivityAge([{ age_seconds: null }]), Infinity);
    assert.equal(logic.projectActivityAge([{}]), Infinity);
    assert.equal(logic.projectActivityAge([]), Infinity);
    assert.equal(logic.projectActivityAge(), Infinity);
    // A mistyped age arrives as NaN (buildSession floors whatever the snapshot
    // carried); letting it through would make the comparator return NaN and leave
    // the panel order undefined.
    assert.equal(logic.projectActivityAge([{ age_seconds: NaN }]), Infinity);
    assert.equal(logic.projectActivityAge([{ age_seconds: NaN }, { age_seconds: 7 }]), 7);
});

test('sortProjects: a project whose age did not parse sorts behind one with a real age', () => {
    const projects = [project('alpha', ['completed'], [NaN]), project('zeta', ['completed'], [86400])];
    assert.deepEqual(logic.sortProjects(projects, true).map((p) => p.name), ['zeta', 'alpha']);
    // Two ageless projects compare equal (Infinity), so the name decides - the
    // comparator must never subtract them into a NaN.
    const ageless = [project('zeta', ['completed'], [NaN]), project('alpha', ['completed'])];
    assert.deepEqual(logic.sortProjects(ageless, true).map((p) => p.name), ['alpha', 'zeta']);
});

test('sortProjects: within a band the freshest project comes first, not the first alphabetically', () => {
    const projects = [
        project('alpha', ['completed'], [86400]),
        project('repo', ['completed'], [60]),
        project('knowledge', ['awaiting_input'], [3600]),
    ];
    assert.deepEqual(logic.sortProjects(projects, true).map((p) => p.name), ['repo', 'knowledge', 'alpha']);
});

test('sortProjects: a panel leaving the busy band lands next to its old spot, not mid-list', () => {
    // The panel that was just working (age 5s) finishes: it drops from the busy
    // band into the quiet one, but as the freshest project there it lands right
    // below the remaining busy panels - one position down, still in view. Under an
    // alphabetical order inside the band it would have jumped to the end.
    const busy = [
        project('zeta', ['working'], [5]),
        project('yard', ['working'], [30]),
        project('alpha', ['completed'], [86400]),
        project('beta', ['awaiting_input'], [7200]),
    ];
    assert.deepEqual(logic.sortProjects(busy, true).map((p) => p.name), ['zeta', 'yard', 'beta', 'alpha']);

    const done = [
        project('zeta', ['awaiting_input'], [5]),
        project('yard', ['working'], [30]),
        project('alpha', ['completed'], [86400]),
        project('beta', ['awaiting_input'], [7200]),
    ];
    assert.deepEqual(logic.sortProjects(done, true).map((p) => p.name), ['yard', 'zeta', 'beta', 'alpha']);
});

test('sortProjects: a uniformly ticking age does not reorder panels', () => {
    // Every panel ages at the same rate, so the relative order is unchanged until
    // a panel actually gains activity.
    const now = [project('alpha', ['completed'], [120]), project('repo', ['completed'], [30])];
    assert.deepEqual(logic.sortProjects(now, true).map((p) => p.name), ['repo', 'alpha']);
    const later = [project('alpha', ['completed'], [420]), project('repo', ['completed'], [330])];
    assert.deepEqual(logic.sortProjects(later, true).map((p) => p.name), ['repo', 'alpha']);
});

test('sortProjects: fine-grained churn inside a band does not reorder panels', () => {
    // build is the fresher of the two, so it leads the busy band regardless of the
    // alphabet.
    const before = logic.sortProjects([project('build', ['working'], [10]), project('app', ['processing'], [90])], true);
    assert.deepEqual(before.map((p) => p.name), ['build', 'app']);
    // app flips processing <-> working (same busy band): order must be unchanged.
    const after = logic.sortProjects([project('build', ['working'], [10]), project('app', ['working'], [90])], true);
    assert.deepEqual(after.map((p) => p.name), ['build', 'app']);
});

test('sortProjects: without priority it is a plain alphabetical list', () => {
    // Neither the band nor the activity age has any say here - that is the point
    // of the toggle being off.
    const projects = [project('zeta', ['awaiting_permission'], [5]), project('alpha', ['completed'], [86400])];
    assert.deepEqual(logic.sortProjects(projects, false).map((p) => p.name), ['alpha', 'zeta']);
});

test('sortProjects: does not mutate its input', () => {
    const projects = [project('b', ['completed']), project('a', ['completed'])];
    logic.sortProjects(projects, true);
    assert.deepEqual(projects.map((p) => p.name), ['b', 'a']);
});

/* --- HTML safety ---

   These are the guard tests for the markup layer: the controller concatenates HTML
   strings, so `esc` (text position) and `attr` (a whole attribute) are the only
   two things standing between a session title, a project path or a background
   task's output and the page. Do not weaken them.

   `changelogUrl` is guarded here too, because a URL-bearing attribute is the one
   place where escaping is not enough: `javascript:alert(1)` needs no metacharacter
   at all, so `attr('href', ...)` would happily quote it. What keeps the href safe
   is that the value is rebuilt from a strict pattern, never passed through. */

test('esc: escapes every character that can break out of text or a quoted attribute', () => {
    assert.equal(logic.esc('&<>"\''), '&amp;&lt;&gt;&quot;&#39;');
});

test('esc: the apostrophe is escaped, so a single-quoted attribute cannot be broken out of', () => {
    // The historical gap: esc() covered " but not ', so attr='...' looked safe
    // and was not. attr() below removes the choice, but esc() has to hold too.
    const escaped = logic.esc("' onmouseover=alert(1) x='");
    assert.equal(escaped.includes("'"), false);   // no raw apostrophe survives to close the value
    assert.equal("<span data-x='" + escaped + "'>", "<span data-x='&#39; onmouseover=alert(1) x=&#39;'>");
});

test('esc: escapes in a single pass, so an entity in the input is not double-escaped', () => {
    // & -> &amp; exactly once: a chained-replace implementation that ran the
    // & step last would turn < into &amp;lt; and print the entity verbatim.
    assert.equal(logic.esc('a & <b>'), 'a &amp; &lt;b&gt;');
    assert.equal(logic.esc('&amp;'), '&amp;amp;');
});

test('esc: null, undefined and numbers become plain strings', () => {
    assert.equal(logic.esc(null), '');
    assert.equal(logic.esc(undefined), '');
    assert.equal(logic.esc(0), '0');
    assert.equal(logic.esc(42), '42');
});

test('attr: emits one double-quoted attribute with a leading space', () => {
    assert.equal(logic.attr('data-session', 'abc'), ' data-session="abc"');
});

test('attr: a value cannot close the attribute or the tag', () => {
    const built = logic.attr('data-title', '" onfocus=alert(1) autofocus="');
    assert.equal(built, ' data-title="&quot; onfocus=alert(1) autofocus=&quot;"');
    // Exactly the two delimiters attr() itself wrote: the value contributed no
    // quote of its own, so onfocus stays text inside the value, not an attribute.
    assert.equal((built.match(/"/g) || []).length, 2);
});

test('attr: a value cannot inject a nested element', () => {
    const tag = '<span' + logic.attr('data-tip', '<img src=x onerror=alert(1)>') + '>';
    assert.equal(tag.includes('<img'), false);
});

test('attr: null and undefined yield an empty value, not the literal word', () => {
    assert.equal(logic.attr('data-x', null), ' data-x=""');
    assert.equal(logic.attr('data-x', undefined), ' data-x=""');
});

test('attr: a computed attribute name is a programming error, not silent markup', () => {
    // No value escaping can save an attribute whose NAME came from data, so the
    // name has to be a literal identifier - anything else throws.
    assert.throws(() => logic.attr('data-x" onload="alert(1)', 'v'), TypeError);
    assert.throws(() => logic.attr('data x', 'v'), TypeError);
    assert.throws(() => logic.attr('', 'v'), TypeError);
    assert.throws(() => logic.attr('2fast', 'v'), TypeError);
    assert.doesNotThrow(() => logic.attr('aria-pressed', 'true'));
});

test('changelogUrl: a release number links, and nothing else does', () => {
    // GitHub's anchor for the "## 2.1.224" heading drops the dots.
    assert.equal(
        logic.changelogUrl('2.1.224'),
        'https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md#21224',
    );
    assert.equal(logic.changelogUrl('10.0.1'), 'https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md#1001');
});

test('changelogUrl: the version reaches the href only as digits and dots', () => {
    // The version comes off disk, so anything that is not exactly three
    // dot-separated numbers must yield no URL at all - no scheme of its own, no
    // attribute break-out, no path traversal, and no partial URL either.
    for (const bad of [
        '2.1', '2.1.224.1', '2.1.224-beta', ' 2.1.224', '2.1.224 ',
        'javascript:alert(1)', 'JavaScript:alert(1)', 'data:text/html,<script>alert(1)</script>',
        '2.1.224" onmouseover="alert(1)', "2.1.224' onmouseover='alert(1)", '2.1.2#a',
        '../../etc', '2.1.2/../../x', '2.1.2 ', '2.1.2\n',
        '', null, undefined, 21224, {}, [],
    ]) {
        assert.equal(logic.changelogUrl(bad), null, 'must not link: ' + String(bad));
    }
});

test('diffToHtml: a diff becomes one classed line each, without the file header', () => {
    const diff = 'diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n\\ No newline at end of file\n';
    assert.equal(logic.diffToHtml(diff),
        '<div class="diff-line diff-hunk">@@ -1,2 +1,2 @@</div>'
        + '<div class="diff-line diff-ctx"> keep</div>'
        + '<div class="diff-line diff-del">-old</div>'
        + '<div class="diff-line diff-add">+new</div>'
        + '<div class="diff-line diff-meta">\\ No newline at end of file</div>');
    assert.equal(logic.diffToHtml('diff --git a/i b/i\nBinary files a/i and b/i differ\n'),
        '<div class="diff-line diff-meta">Binary files a/i and b/i differ</div>');
    assert.equal(logic.diffToHtml(''), '');
    assert.equal(logic.diffToHtml(null), '');
});

test('diffToHtml: markup in a file stays text and never reaches a class', () => {
    const html = logic.diffToHtml('@@ -1 +1 @@\n+<script>alert(1)</script> & "q" \'s\'\n<img src=x onerror=alert(1)>\n');
    assert.ok(!html.includes('<script') && !html.includes('<img'));
    assert.ok(html.includes('+&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot; &#39;s&#39;'));
    // A line of any other first character is context, never a class taken from the text.
    assert.ok(html.includes('<div class="diff-line diff-ctx">&lt;img src=x onerror=alert(1)&gt;</div>'));
    for (const match of html.matchAll(/class="([^"]*)"/g)) {
        assert.ok(/^diff-line diff-(add|del|ctx|hunk|meta)$/.test(match[1]), match[1]);
    }
});

test('ansiToHtml: markup in process output stays text', () => {
    const html = logic.ansiToHtml('<script>alert(1)</script> & "quoted" \'single\'');
    assert.equal(html, '&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quoted&quot; &#39;single&#39;');
});

test('ansiToHtml: known color codes become fixed classes', () => {
    assert.equal(logic.ansiToHtml('\x1b[31mred\x1b[0m plain'), '<span class="ansi-red">red</span> plain');
    assert.equal(logic.ansiToHtml('\x1b[1;32mok\x1b[0m'), '<span class="ansi-green ansi-bold">ok</span>');
});

test('ansiToHtml: an unmapped code emits no class and never leaks its digits', () => {
    // 38;5;196 (256-color) is deliberately not mapped: the raw parameters must
    // not reach the class attribute, and the sequence itself must be dropped.
    const html = logic.ansiToHtml('\x1b[38;5;196mtext');
    assert.equal(html, 'text');
});

test('ansiToHtml: cursor and erase sequences are dropped, not rendered', () => {
    assert.equal(logic.ansiToHtml('a\x1b[2K\x1b[1;5Hb'), 'ab');
});

test('ansiToHtml: a crafted sequence cannot break out of the class attribute', () => {
    const html = logic.ansiToHtml('\x1b[31m"><img src=x>\x1b[0m');
    assert.equal(html, '<span class="ansi-red">&quot;&gt;&lt;img src=x&gt;</span>');
});

test('ansiToHtml: empty and missing input yield empty markup', () => {
    assert.equal(logic.ansiToHtml(''), '');
    assert.equal(logic.ansiToHtml(null), '');
    assert.equal(logic.ansiToHtml(undefined), '');
});

test('empty-state and notice markup keep hostile label text out of the markup', () => {
    // Labels come from locale files on disk and the count is interpolated like
    // any other value, so both go through esc/attr - numbers included.
    const labels = {
        search_no_match: '<img src=x onerror=alert(1)>',
        search_scope_hint: '</div><script>x</script>',
        search_widen_hidden: '"><script>x</script>',
        search_widened_note: '{count}',
        search_widened_reset: '"><b>go</b>',
        search_widened_reset_tip: '" onmouseover="alert(1)',
    };

    const empty = logic.emptyStateMarkup({
        allKeys: ['a'], activeKeys: [], includeHidden: false, historyRange: '24h',
        searchActive: true, labels,
    });
    assert.ok(!empty.includes('<img'));
    assert.ok(!empty.includes('<script'));
    assert.ok(!empty.includes('</div><'));
    // The one attribute the block writes is still the one it meant to write.
    assert.match(empty, /data-widen="hidden"/);

    // A hostile count is escaped too - the rule is every interpolated value, so
    // no call site has to decide whether a "number" needs escaping.
    const notice = logic.widenedNoticeMarkup('<b>1</b>', labels);
    assert.ok(!notice.includes('<b>'));
    assert.match(notice, /&lt;b&gt;1&lt;\/b&gt;/);
    // The tooltip's quote is escaped, so it cannot close the attribute it sits in.
    assert.ok(!notice.includes('" onmouseover='));
    assert.match(notice, /data-tip="&quot; onmouseover=&quot;alert\(1\)"/);
});

test('search-hit markup keeps transcript text and tool names out of the markup', () => {
    // This is the one place transcript text reaches the page, so every piece of
    // it is hostile input: the excerpt is whatever the conversation contained,
    // the tool name is whatever the entry called itself, and the count is
    // interpolated like any other value.
    const html = logic.searchHitsMarkup({
        count: 2,
        snippets: [
            {
                kind: 'user',
                before: '</span><script>x</script>',
                match: '<img src=x onerror=alert(1)>',
                after: '" onmouseover="alert(1)',
            },
            { kind: 'tool_use', tool: '<svg onload=alert(1)>', before: '', match: 'y', after: '' },
        ],
    }, { search_hits_count: 'Found: {count}', search_hit_user: 'You' });

    assert.ok(!html.includes('<script'));
    assert.ok(!html.includes('<img'));
    assert.ok(!html.includes('<svg'));
    assert.ok(!html.includes('" onmouseover='));
    // The excerpt's own closing tag is escaped rather than closing the span it
    // sits in - the renderer's own </span> is the only real one in the output.
    assert.match(html, /&lt;\/span&gt;&lt;script&gt;/);
    // The only markup around a hit is the <mark> the renderer itself writes.
    assert.match(html, /<mark>&lt;img src=x onerror=alert\(1\)&gt;<\/mark>/);

    // The count is the one interpolated value that never reaches esc as given:
    // anything but a real number produces no block at all, which is a stronger
    // guarantee than escaping it would be.
    assert.equal(logic.searchHitsMarkup({ count: '<b>2</b>', snippets: [] }, {}), '');
});

test('the search-hit kind lookup cannot answer with an inherited value', () => {
    // `kind` is backend data like every other on-disk token here, so the lookup
    // is a Map: an object would resolve `constructor` to an inherited value and
    // render a function into the row.
    const html = logic.searchHitsMarkup({
        count: 1,
        snippets: [{ kind: 'constructor', before: '', match: 'x', after: '' }],
    }, { search_hits_count: 'Found: {count}' });

    assert.ok(!html.includes('function'));
    assert.ok(!html.includes('Object'));
    assert.ok(!html.includes('hit-source'));
    assert.match(html, /<mark>x<\/mark>/);
});

/* --- session details (branch, context, plan, queue, budget, hooks, PRs) --- */

const DETAIL_LABELS = {
    branch_tip: 'Git branch: {branch}',
    context_tip: 'Context: {used} of {size} tokens ({percent}%)',
    context_used: 'Context: {used} tokens',
    compactions: 'Context compactions: {count}',
    files_edited: 'Files edited: {count}',
    artifacts: 'Artifacts: {count}',
};

test('context usage: share of the catalog window, capped at full', () => {
    assert.deepEqual(logic.contextUsage({ context_tokens: 250000, context_window: 1000000 }), { used: 250000, size: 1000000, ratio: 0.25 });
    assert.equal(logic.contextUsage({ context_tokens: 1200000, context_window: 1000000 }).ratio, 1);
});

test('context usage: without a window the size is known but not the share', () => {
    assert.deepEqual(logic.contextUsage({ context_tokens: 5000 }), { used: 5000, size: null, ratio: null });
});

test('context usage: a missing or mistyped figure shows nothing', () => {
    for (const value of [undefined, null, 0, -3, 'abc', true, 1.5]) {
        assert.equal(logic.contextUsage({ context_tokens: value, context_window: 1000000 }), null);
    }
});

test('plan progress: the step in progress of a known step count', () => {
    assert.deepEqual(logic.planProgress({ plan_step: 3, plan_total: 7 }), { step: 3, total: 7 });
    assert.equal(logic.planProgress({ plan_step: 8, plan_total: 7 }), null);
    assert.equal(logic.planProgress({ plan_step: null, plan_total: 0 }), null);
    assert.equal(logic.planProgress({ plan_step: true, plan_total: 2 }), null);
});

test('budget: used and total, only with a positive total', () => {
    assert.deepEqual(logic.budgetOf({ budget_used: 12, budget_total: 60 }), { used: 12, total: 60, ratio: 0.2 });
    assert.equal(logic.budgetOf({ budget_used: 1, budget_total: 0 }), null);
    assert.equal(logic.budgetOf({ budget_used: true, budget_total: 60 }), null);
    assert.equal(logic.budgetOf({}), null);
});

test('hook errors: malformed entries are dropped, an unknown exit code stays unknown', () => {
    assert.deepEqual(logic.hookErrorsOf({ hook_errors: [
        { hook: 'PostToolUse:Bash', exit_code: 7, count: 3, message: 'lint failed' },
        { hook: 'Stop', exit_code: 'x', count: 1, message: 7 },
        { hook: '', exit_code: 1, count: 1 },
        { hook: 'Zero', exit_code: 1, count: 0 },
        null,
    ] }), [
        { hook: 'PostToolUse:Bash', exit_code: 7, count: 3, message: 'lint failed' },
        { hook: 'Stop', exit_code: null, count: 1, message: '' },
    ]);
    assert.deepEqual(logic.hookErrorsOf({ hook_errors: 'nope' }), []);
});

test('effort and fast mode: only plain tokens are shown', () => {
    assert.equal(logic.effortOf({ effort: 'xhigh' }), 'xhigh');
    assert.equal(logic.effortOf({ effort: '<b>max</b>' }), '');
    assert.equal(logic.effortOf({ effort: 3 }), '');
    assert.equal(logic.isFastMode({ speed: 'fast' }), true);
    assert.equal(logic.isFastMode({ speed: 'standard' }), false);
});

test('panel branch: the branch most sessions are on, the first seen winning a tie', () => {
    assert.equal(logic.panelBranch([{ branch: 'feature/x' }, { branch: '' }, { branch: 'main' }, { branch: 'main' }]), 'main');
    assert.equal(logic.panelBranch([{ branch: 'main' }, { branch: 'feature/x' }]), 'main');
    assert.equal(logic.panelBranch([{ branch: '' }]), null);
});

test('group projects: the main branch goes to the header, only a row elsewhere names its own', () => {
    const groups = logic.groupProjects([
        raw({ session_id: 'a', pid: 1, cwd: 'c:\\p', git_branch: 'main' }),
        raw({ session_id: 'b', pid: 2, cwd: 'c:\\p', git_branch: 'main' }),
        raw({ session_id: 'c', pid: 3, cwd: 'c:\\p', git_branch: 'feature/x' }),
        // The worktree row names its branch in the worktree badge already.
        raw({ session_id: 'd', pid: 4, cwd: 'c:\\p', git_branch: 'wt', worktree_cwd: 'c:\\p\\wt', worktree_branch: 'wt' }),
        raw({ session_id: 'e', pid: 5, cwd: 'c:\\p' }),
    ], {}, {});
    assert.equal(groups[0].branch, 'main');
    assert.deepEqual(groups[0].sessions.map((session) => session.show_branch), [false, false, true, false, false]);
});

test('branchOf leaves out the branch Claude Desktop named after the worktree folder', () => {
    const worktree = '/home/jens/dev/nova/.claude/worktrees/beautiful-heyrovsky-9c2e00';
    // Desktop starts the chat in its own worktree on claude/<folder>: the panel path says as much.
    assert.equal(logic.branchOf(raw({ cwd: worktree, git_branch: 'claude/beautiful-heyrovsky-9c2e00' })), '');
    assert.equal(logic.branchOf(raw({ cwd: worktree + '/', git_branch: 'claude/beautiful-heyrovsky-9c2e00' })), '');
    // The same for a session that switched into the worktree, and for a Windows path.
    assert.equal(logic.branchOf(raw({ cwd: '/home/jens/dev/nova', worktree_cwd: worktree, git_branch: 'claude/beautiful-heyrovsky-9c2e00' })), '');
    assert.equal(logic.branchOf(raw({ cwd: 'C:\\dev\\x\\.claude\\worktrees\\calm-otter-1a2b3c', git_branch: 'claude/calm-otter-1a2b3c' })), '');

    // A branch of the agent's own, a claude/ branch of another folder, or a folder alone is shown as it is.
    assert.equal(logic.branchOf(raw({ cwd: worktree, git_branch: 'bug/JDUT/Examine-the-stack-root' })), 'bug/JDUT/Examine-the-stack-root');
    assert.equal(logic.branchOf(raw({ cwd: worktree, git_branch: 'claude/epic-cray-6b3840' })), 'claude/epic-cray-6b3840');
    assert.equal(logic.branchOf(raw({ cwd: worktree, git_branch: 'beautiful-heyrovsky-9c2e00' })), 'beautiful-heyrovsky-9c2e00');
    assert.equal(logic.branchOf(raw({ cwd: '/', git_branch: 'claude/' })), 'claude/');
    assert.equal(logic.branchOf(raw({ cwd: worktree, git_branch: 7 })), '');
});

test('build session: details of a running process only while it is alive', () => {
    const fields = { plan_step: 2, plan_total: 4, hook_errors: [{ hook: 'Stop', exit_code: 1, count: 1 }] };
    const live = logic.buildSession(raw(fields), {}, {});
    assert.deepEqual(live.plan, { step: 2, total: 4 });
    assert.equal(live.hook_errors.length, 1);

    const ended = logic.buildSession(raw(Object.assign({ alive: false }, fields)), {}, {});
    assert.equal(ended.plan, null);
    assert.deepEqual(ended.hook_errors, []);
});

test('build session: queued prompts count only while a turn runs', () => {
    assert.equal(logic.buildSession(raw({ last_entry_kind: 'user_text', queued: 2 }), {}, {}).queued, 2);
    assert.equal(logic.buildSession(raw({ last_entry_kind: 'assistant', last_stop_reason: 'end_turn', queued: 2 }), {}, {}).queued, 0);
});

test('build session: the context warns from the near-limit share', () => {
    const near = Math.ceil(logic.NEAR_LIMIT_RATIO * 1000000);
    assert.equal(logic.buildSession(raw({ context_tokens: near, context_window: 1000000 }), {}, {}).context_warn, true);
    assert.equal(logic.buildSession(raw({ context_tokens: near - 1000, context_window: 1000000 }), {}, {}).context_warn, false);
    assert.equal(logic.buildSession(raw({ context_tokens: near }), {}, {}).context_warn, false);
});

const CACHE_LABELS = {
    age_seconds: '{s}s',
    age_minutes: '{m}m',
    cache_warm_tip: 'Warm for {time}',
    cache_cold_tip: 'Expired: {tokens} again ({cost})',
    cache_cold_tokens_tip: 'Expired: {tokens} again',
};

// An idle session whose cache, written with a one-hour lifetime, lapsed ten minutes ago.
function coldCache(overrides) {
    return raw(Object.assign({
        last_entry_kind: 'assistant', last_stop_reason: 'end_turn', model_id: 'claude-opus-4-8',
        context_tokens: 400000, cache_ttl_seconds: 3600, cache_expires_in_seconds: -600,
    }, overrides));
}

test('cache outlook: a lapsed cache costs its context at the write rate of its lifetime', () => {
    assert.deepEqual(logic.cacheOutlook(coldCache(), PRICES), { warm: false, remaining_seconds: 0, tokens: 400000, rewrite_usd: 4 });
    assert.equal(logic.cacheOutlook(coldCache({ cache_ttl_seconds: 300 }), PRICES).rewrite_usd, 2.5);
});

test('cache outlook: a warm cache counts down to its expiry', () => {
    const cache = logic.cacheOutlook(coldCache({ cache_expires_in_seconds: 1500 }), PRICES);
    assert.equal(cache.warm, true);
    assert.equal(cache.remaining_seconds, 1500);
    assert.equal(logic.cacheLine(cache, CACHE_LABELS), 'Warm for 25m');
});

test('cache outlook: an unpriced or hostile model keeps the tokens but names no cost', () => {
    for (const model of ['claude-unknown-9', 'constructor', '__proto__', null]) {
        const cache = logic.cacheOutlook(coldCache({ model_id: model }), PRICES);
        assert.equal(cache.rewrite_usd, null);
        assert.equal(logic.cacheLine(cache, CACHE_LABELS), 'Expired: 400k again');
    }
});

test('cache outlook: a missing or mistyped figure shows nothing', () => {
    for (const fields of [
        { cache_ttl_seconds: null }, { cache_ttl_seconds: true }, { context_tokens: 0 },
        { cache_expires_in_seconds: null }, { cache_expires_in_seconds: true }, { cache_expires_in_seconds: 'soon' },
    ]) {
        assert.equal(logic.cacheOutlook(coldCache(fields), PRICES), null);
    }
});

test('build session: a lapsed cache earns a badge only when your next prompt pays for it', () => {
    const idle = logic.buildSession(coldCache(), CACHE_LABELS, PRICES);
    assert.equal(idle.cache_warn, true);
    assert.equal(idle.cost_tip.split('\n').pop(), 'Expired: 400k again ($4)');

    // Below the threshold the line stays in the tooltip.
    const cheap = logic.buildSession(coldCache({ context_tokens: 50000 }), CACHE_LABELS, PRICES);
    assert.equal(cheap.cache_warn, false);
    assert.equal(cheap.cost_tip, 'Expired: 50.0k again (<$1)');

    // A running turn keeps its cache warm, and an ended session has none to pay for.
    for (const fields of [{ last_entry_kind: 'user_text', last_stop_reason: null }, { alive: false }]) {
        const session = logic.buildSession(coldCache(fields), CACHE_LABELS, PRICES);
        assert.equal(session.cache, null);
        assert.equal(session.cache_warn, false);
    }
});

test('build session: the cache line does not wait for a pending scan', () => {
    const session = logic.buildSession(coldCache({ scan_pending: true }), CACHE_LABELS, PRICES);
    assert.equal(session.cost_tip, 'Expired: 400k again ($4)');
});

test('pull request addresses: only a plain https address may reach an href', () => {
    assert.equal(logic.pullRequestUrl('https://gitlab.example.com/group/repo/-/merge_requests/217'),
        'https://gitlab.example.com/group/repo/-/merge_requests/217');
    assert.equal(logic.pullRequestUrl('https://github.com/o/r/pull/5/'), 'https://github.com/o/r/pull/5/');
    for (const hostile of [
        'javascript:alert(1)',
        'http://example.com/pr/1',
        'https://example.com/pr/1?x=1',
        'https://example.com/pr/1#x',
        'https://user:pass@example.com/pr/1',
        'https://example.com/pr/"onmouseover="alert(1)',
        'https://example.com/pr/<script>',
        'https://example.com/a b',
        ' https://example.com/pr/1',
        'https://example.com/' + 'a'.repeat(500),
        42,
        null,
    ]) {
        assert.equal(logic.pullRequestUrl(hostile), null, String(hostile));
    }
});

test('pull requests: a hostile address is kept as a listing without a link', () => {
    assert.deepEqual(logic.pullRequestsOf({ pr_links: [
        { repository: 'group/repo', number: 3, url: 'javascript:alert(1)' },
        { repository: 'group/repo', number: 0, url: null },
        { repository: '', number: 4 },
    ] }), [{ repository: 'group/repo', number: 3, url: null }]);
});

/* --- cost parts --- */

const COST_LABELS = {
    cost_main: 'Main: {cost}',
    cost_agents: 'Agents: {cost}',
    cost_agent_unknown: 'other',
    cost_skills: 'Skills:',
    cost_web_search: 'Searches: {count} · {cost}',
};

// One million output tokens on opus-4-8 cost $25.
const MTOK_OUT = { output_tokens: 1000000 };

test('sessionTotalCostUsd: adds the web searches to the tokens', () => {
    const prices = Object.assign({ web_search: { per_request: 0.01 } }, PRICES);
    const session = { usage_by_model: { 'claude-opus-4-8': MTOK_OUT }, usage_parts: { web_searches: 300 } };

    assert.equal(logic.sessionTotalCostUsd(session, prices), 28);
    assert.equal(logic.sessionTotalCostUsd(session, PRICES), 25, 'no price for a search adds nothing');
    assert.equal(logic.sessionTotalCostUsd({ usage_by_model: { 'claude-unknown-9': MTOK_OUT } }, prices), null);
});

test('webSearchPrice: only a finite, non-negative per-request price counts', () => {
    assert.equal(logic.webSearchPrice({ web_search: { per_request: 0.01 } }), 0.01);
    for (const entry of [undefined, null, {}, { per_request: -1 }, { per_request: 'x' }, 'cheap']) {
        assert.equal(logic.webSearchPrice({ web_search: entry }), null, JSON.stringify(entry));
    }
    assert.equal(logic.webSearchPrice(Object.create({ web_search: { per_request: 1 } })), null, 'inherited entry');
});

test('costTipLines: splits a session that ran subagents into its parts', () => {
    const prices = Object.assign({ web_search: { per_request: 0.01 } }, PRICES);
    const lines = logic.costTipLines({
        usage_parts: {
            main: { 'claude-opus-4-8': MTOK_OUT },
            agents: {
                'next:reviewer': { 'claude-opus-4-8': { output_tokens: 2000000 } },
                '': { 'claude-opus-4-8': { output_tokens: 40000 } },
            },
            skills: { 'next:review': { 'claude-opus-4-8': { output_tokens: 2000000 } } },
            web_searches: 150,
        },
    }, prices, COST_LABELS);

    assert.deepEqual(lines, [
        'Main: $25',
        'Agents: $51',
        '• next:reviewer $50',
        '• other $1',
        'Skills:',
        '• next:review $50',
        'Searches: 150 · $2',
    ]);
});

test('costTipLines: without subagents there is no main/agent split, and nothing at all means no lines', () => {
    const lines = logic.costTipLines({
        usage_parts: { main: { 'claude-opus-4-8': MTOK_OUT }, agents: {}, skills: {}, web_searches: 0 },
    }, PRICES, COST_LABELS);

    assert.deepEqual(lines, []);
    assert.deepEqual(logic.costTipLines({}, PRICES, COST_LABELS), []);
    assert.deepEqual(logic.costTipLines({ usage_parts: 'broken' }, PRICES, COST_LABELS), []);
});

test('costTipLines: an unpriced part shows its tokens, and a long list counts the rest', () => {
    const skills = {};
    for (const name of ['a', 'b', 'c', 'd', 'e', 'f']) {
        skills[name] = { 'claude-opus-4-8': { output_tokens: 1000000 } };
    }
    skills.u = { 'claude-unknown-9': { output_tokens: 3000 } };
    const lines = logic.costTipLines({ usage_parts: { skills: skills } }, PRICES, COST_LABELS);

    assert.deepEqual(lines, ['Skills:', '• a $25', '• b $25', '• c $25', '• d $25', '• +3']);
});

test('costTipLines: an on-disk name like __proto__ stays a name, never a prototype', () => {
    const parts = JSON.parse('{"agents": {"__proto__": {"claude-opus-4-8": {"output_tokens": 1000000}}},'
        + ' "main": {"__proto__": {"output_tokens": 5}}}');
    const lines = logic.costTipLines({ usage_parts: parts }, PRICES, COST_LABELS);

    assert.deepEqual(lines.slice(1), ['Agents: $25', '• __proto__ $25']);
    assert.equal({}.output_tokens, undefined);
});

/* --- list sections, signals, search over the list, taskbar attention --- */

const ALL_FILTER_KEYS = ['needs', 'errored', 'interrupted', 'new', 'idle', 'working', 'background', 'quiet', 'history'];

test('list sections cover every filter bucket exactly once', () => {
    const buckets = logic.LIST_SECTIONS.flatMap((section) => section.buckets);
    assert.deepEqual([...buckets].sort(), [...ALL_FILTER_KEYS].sort());
    assert.equal(new Set(buckets).size, buckets.length);
    assert.deepEqual(logic.LIST_SECTIONS.map((section) => section.key), ['needs', 'stopped', 'running', 'recent', 'older']);
});

test('sectionOf: a session lands in the section of its status, a past one in "older"', () => {
    assert.equal(logic.sectionOf({ status: 'awaiting_permission' }), 'needs');
    assert.equal(logic.sectionOf({ status: 'errored' }), 'stopped');
    assert.equal(logic.sectionOf({ status: 'interrupted' }), 'stopped');
    assert.equal(logic.sectionOf({ status: 'working' }), 'running');
    assert.equal(logic.sectionOf({ status: 'processing' }), 'running');
    assert.equal(logic.sectionOf({ status: 'awaiting_input' }), 'recent');
    assert.equal(logic.sectionOf({ status: 'new' }), 'recent');
    assert.equal(logic.sectionOf({ status: 'completed' }), 'recent');
    assert.equal(logic.sectionOf({ status: 'completed', is_history: true }), 'older');
    assert.equal(logic.sectionOf({ status: 'bogus' }), null);
});

test('sections open and close all their buckets at once', () => {
    const opened = logic.toggledSectionKeys('stopped', new Set(['needs']));
    assert.deepEqual([...opened].sort(), ['errored', 'interrupted', 'needs']);
    assert.equal(logic.sectionActive('stopped', opened), true);

    const closed = logic.toggledSectionKeys('stopped', opened);
    assert.deepEqual([...closed], ['needs']);
    assert.equal(logic.sectionActive('stopped', closed), false);

    // A selection stored before the sections existed holds only part of one:
    // the section reads open, and a click closes all of it.
    const partial = new Set(['idle']);
    assert.equal(logic.sectionActive('recent', partial), true);
    assert.deepEqual([...logic.toggledSectionKeys('recent', partial)], []);

    // The input is never mutated, and an unknown section changes nothing.
    assert.deepEqual([...partial], ['idle']);
    assert.deepEqual([...logic.toggledSectionKeys('nope', partial)], ['idle']);
});

test('sortSectionSessions: longest wait first, newest first, or by process start', () => {
    const sessions = [
        { name: 'b', session_id: '2', pid: 2, age_seconds: 30, process_age_seconds: 500 },
        { name: 'a', session_id: '1', pid: 1, age_seconds: 300, process_age_seconds: 50 },
        { name: 'c', session_id: '3', pid: 3, age_seconds: null, process_age_seconds: null },
    ];
    const names = (list) => list.map((session) => session.name);

    assert.deepEqual(names(logic.sortSectionSessions(sessions, 'waiting')), ['a', 'b', 'c']);
    assert.deepEqual(names(logic.sortSectionSessions(sessions, 'newest')), ['b', 'a', 'c']);
    assert.deepEqual(names(logic.sortSectionSessions(sessions, 'started')), ['a', 'b', 'c']);
    // The input keeps its order.
    assert.deepEqual(names(sessions), ['b', 'a', 'c']);
});

test('sortSectionSessions: equal keys fall back to name, id and pid, so the order is total', () => {
    const sessions = [
        { name: 'same', session_id: 'b', pid: 1, age_seconds: 10 },
        { name: 'same', session_id: 'a', pid: 2, age_seconds: 10 },
        { name: 'same', session_id: 'a', pid: 1, age_seconds: 10 },
        { name: 'other', session_id: 'z', pid: 9, age_seconds: 10 },
    ];
    const keys = logic.sortSectionSessions(sessions, 'newest').map((session) => session.session_id + session.pid);
    assert.deepEqual(keys, ['z9', 'a1', 'a2', 'b1']);
});

test('buildSections: every section in order, with its sessions and whether it is open', () => {
    const sessions = [
        { name: 'w', status: 'working', age_seconds: 1 },
        { name: 'p', status: 'awaiting_permission', age_seconds: 5 },
        { name: 'h', status: 'completed', is_history: true, age_seconds: 9 },
    ];
    const sections = logic.buildSections(sessions, new Set(['needs', 'working', 'background']));

    assert.deepEqual(sections.map((section) => section.key), ['needs', 'stopped', 'running', 'recent', 'older']);
    assert.deepEqual(sections.map((section) => section.active), [true, false, true, false, false]);
    assert.deepEqual(sections.map((section) => section.sessions.map((session) => session.name)), [['p'], [], ['w'], [], ['h']]);
});

test('flattenProjects: each session carries the project it was grouped under', () => {
    const flat = logic.flattenProjects([
        { key: 'local|d:/p', name: 'p', cwd: 'D:\\p', origin: 'local', origin_display: '', sessions: [{ session_id: 's1' }] },
        { key: 'wsl|/q', name: 'q', cwd: '/q', origin: 'wsl:Ubuntu', origin_display: 'Ubuntu', sessions: [{ session_id: 's2' }] },
    ]);

    assert.deepEqual(flat.map((session) => [session.session_id, session.project_name, session.project_cwd, session.project_origin_display]),
        [['s1', 'p', 'D:\\p', ''], ['s2', 'q', '/q', 'Ubuntu']]);
});

const SIGNAL_LABELS = {
    signal_processes: 'Processes: {count}',
    signal_agents: 'Agents: {count}',
    signal_workflow: 'Workflow {done}/{total}',
    signal_budget: 'Budget {percent}%',
    signal_auto_paused: 'Auto mode paused',
    signal_update: 'Update {version}',
    signal_queued: 'Queued: {count}',
};

test('sessionSignals: the facts worth spelling out, in rank order - the context, the prompt cache and hook errors stay in the details', () => {
    const signals = logic.sessionSignals({
        processes: 2,
        workflow_active: false,
        subagents_running: 4,
        context_warn: true,
        context: { used: 870000, size: 1000000, ratio: 0.87 },
        budget: { used: 41, total: 50, ratio: 0.82 },
        cache_warn: true,
        cache: { rewrite_usd: 2.4 },
        auto_paused: true,
        cli_update: { available: '2.1.228', running: '2.1.223' },
        hook_errors: [{ hook: 'pre', exit_code: 1, count: 10 }, { hook: 'post', exit_code: 2, count: 4 }],
        queued: 1,
    }, SIGNAL_LABELS);

    assert.deepEqual(signals.map((signal) => signal.text), [
        'Processes: 2', 'Agents: 4', 'Budget 82%', 'Auto mode paused', 'Update 2.1.228', 'Queued: 1',
    ]);
    assert.deepEqual(signals.slice(0, logic.LIST_SIGNAL_LIMIT).map((signal) => signal.tone), ['work', 'background']);
});

test('sessionSignals: a workflow names its progress instead of the agent count, a quiet session none', () => {
    const signals = logic.sessionSignals({ workflow_active: true, workflow_done: 6, workflow_total: 12, subagents_running: 3, hook_errors: [] }, SIGNAL_LABELS);
    assert.deepEqual(signals.map((signal) => signal.text), ['Workflow 6/12']);
    assert.deepEqual(logic.sessionSignals({ processes: 0, hook_errors: [] }, SIGNAL_LABELS), []);
});

test('sessionMetaMatch: title, project, branch and the start of the id, without a file', () => {
    const session = {
        name: 'Port the ReSTIR denoiser', project_name: 'helios-renderer', branch: 'fix/HEL-214-hot-reload',
        session_id: '4c8e1f20-9b7a-4d3e-8f61-2a5c0e9d7b13',
    };
    assert.equal(logic.sessionMetaMatch(session, 'restir', {}), 'title');
    assert.equal(logic.sessionMetaMatch(session, 'helios', {}), 'project');
    assert.equal(logic.sessionMetaMatch(session, 'HEL-214', {}), 'branch');
    assert.equal(logic.sessionMetaMatch(session, '4c8e1f20-9b7a', {}), 'id');
    assert.equal(logic.sessionMetaMatch(session, '4C8E', {}), 'id');
    // A short hex run anywhere in the id is not a match; the id only matches from its start.
    assert.equal(logic.sessionMetaMatch(session, '9b7a', {}), null);
    assert.equal(logic.sessionMetaMatch(session, '4c8', {}), null);
    assert.equal(logic.sessionMetaMatch(session, 'nothing', {}), null);
    assert.equal(logic.sessionMetaMatch(session, '   ', {}), null);
});

test('sessionMetaMatch: the editor options apply, an invalid pattern matches nothing', () => {
    const session = { name: 'Port the ReSTIR denoiser', project_name: 'helios', branch: '', session_id: 'abcd' };
    assert.equal(logic.sessionMetaMatch(session, 'restir', { match_case: true }), null);
    assert.equal(logic.sessionMetaMatch(session, 'ReSTIR', { match_case: true }), 'title');
    assert.equal(logic.sessionMetaMatch(session, 'deno', { whole_word: true }), null);
    assert.equal(logic.sessionMetaMatch(session, 'denoiser', { whole_word: true }), 'title');
    assert.equal(logic.sessionMetaMatch(session, 'Re.TIR', { use_regex: true }), 'title');
    assert.equal(logic.sessionMetaMatch(session, 'Re.TIR', {}), null);
    assert.equal(logic.sessionMetaMatch(session, '(', { use_regex: true }), null);
    assert.equal(logic.searchRegExp('(', { use_regex: true }), null);
});

test('attentionUpdate: counts the sessions blocked on you, flashes only for a newcomer', () => {
    const blocked = (id) => ({ session_id: id, pid: 1, status: 'awaiting_permission' });
    const first = logic.attentionUpdate(null, [blocked('a'), { session_id: 'b', pid: 2, status: 'working' }]);
    assert.equal(first.count, 1);
    assert.equal(first.newlyWaiting, false);

    const same = logic.attentionUpdate(first.keys, [blocked('a')]);
    assert.equal(same.newlyWaiting, false);

    const more = logic.attentionUpdate(same.keys, [blocked('a'), blocked('c')]);
    assert.equal(more.count, 2);
    assert.equal(more.newlyWaiting, true);

    const fewer = logic.attentionUpdate(more.keys, [blocked('c')]);
    assert.equal(fewer.count, 1);
    assert.equal(fewer.newlyWaiting, false);

    // A past session is never counted, whatever its status.
    assert.equal(logic.attentionUpdate(null, [{ session_id: 'h', pid: 0, status: 'awaiting_permission', is_history: true }]).count, 0);
});

test('searchHitsMarkup: a limit shows the first excerpts and counts the rest as not shown', () => {
    const hit = {
        count: 3,
        snippets: [
            { kind: 'user', before: 'a ', match: 'x', after: ' b' },
            { kind: 'user', before: 'c ', match: 'x', after: ' d' },
        ],
    };
    const labels = { search_hits_count: 'Hits: {count}', search_hits_more: 'Not shown: {count}' };
    const limited = logic.searchHitsMarkup(hit, labels, 1);
    assert.equal((limited.match(/<mark>/g) || []).length, 1);
    assert.ok(limited.includes('Not shown: 2'));

    const all = logic.searchHitsMarkup(hit, labels);
    assert.equal((all.match(/<mark>/g) || []).length, 2);
    assert.ok(all.includes('Not shown: 1'));
});

test('costShares: the main conversation against its subagents, only when both are priced', () => {
    const usage = (input) => ({ input_tokens: input, output_tokens: 0 });
    const shares = logic.costShares({
        usage_parts: {
            main: { 'claude-opus-4-8': usage(1000000) },
            agents: { Explore: { 'claude-haiku-4-5': usage(2000000) } },
        },
    }, PRICES);
    assert.deepEqual(shares, { main: 5, agents: 2 });

    assert.equal(logic.costShares({ usage_parts: { main: { 'claude-opus-4-8': usage(1) }, agents: {} } }, PRICES), null);
    assert.equal(logic.costShares({ usage_parts: { main: { 'mystery-model': usage(1) }, agents: { x: { 'claude-opus-4-8': usage(1) } } } }, PRICES), null);
    assert.equal(logic.costShares({}, PRICES), null);
});

test('artifactsOf: links only a safe address, and names an untitled artifact by its address', () => {
    const artifacts = logic.artifactsOf({ artifacts: [
        { title: 'Concepts', url: 'https://claude.ai/artifact/a1' },
        { title: '', url: 'https://claude.ai/artifact/b2' },
        { title: 'Hostile', url: 'javascript:alert(1)' },
        { title: 'Quoted', url: 'https://claude.ai/artifact/"onmouseover' },
        { title: 'No address' },
        null,
    ] });
    assert.deepEqual(artifacts, [
        { title: 'Concepts', url: 'https://claude.ai/artifact/a1' },
        { title: 'https://claude.ai/artifact/b2', url: 'https://claude.ai/artifact/b2' },
        { title: 'Hostile', url: null },
        { title: 'Quoted', url: null },
    ]);
    assert.deepEqual(logic.artifactsOf({}), []);
    assert.deepEqual(logic.artifactsOf({ artifacts: 'nope' }), []);
});

/* --- monitoring: current call, activity line, plan, lines, worked time, tests, graph --- */

const LIVE = { alive: true, has_transcript: true, has_activity: true, age_seconds: 5, process_age_seconds: 600 };

test('current call: a pending command names itself, a question carries its answers', () => {
    const call = logic.currentToolOf({ current_tool: { tool: 'Bash', text: 'npm test', detail: 'Run tests', questions: [] }, current_tool_age_seconds: 12 });
    assert.equal(call.name, 'Bash');
    assert.equal(call.age_seconds, 12);
    assert.equal(logic.toolCallLine(call), 'Bash · npm test');
    const ask = logic.currentToolOf({ current_tool: { tool: 'AskUserQuestion', questions: [
        { text: 'Which?', options: ['A', 3, 'B'] }, { text: '', options: [] }, null,
    ] } });
    assert.deepEqual(ask.questions, [{ text: 'Which?', options: ['A', 'B'] }]);
    assert.equal(logic.currentToolOf({ current_tool: { tool: '' } }), null);
    assert.equal(logic.currentToolOf({ current_tool: 'Bash' }), null);
    assert.equal(logic.toolDisplayName('mcp__github__create_issue'), 'create_issue');
    const agent = logic.currentToolOf({ current_tool: { tool: 'Agent', text: 'Explore', detail: 'Find the parser' } });
    assert.equal(logic.toolCallLine(agent), 'Agent · Explore: Find the parser');
});

test('activity line: what it waits on, what it runs, or what it last said', () => {
    const labels = { activity_plan: 'Plan to review' };
    const bash = logic.currentToolOf({ current_tool: { tool: 'Bash', text: 'npm test\nmore' }, current_tool_age_seconds: 30 });
    assert.deepEqual(logic.activityOf({}, 'awaiting_permission', bash, labels), { kind: 'tool', text: 'Bash · npm test', since: null });
    assert.deepEqual(logic.activityOf({}, 'working', bash, labels), { kind: 'tool', text: 'Bash · npm test', since: 30 });
    const ask = logic.currentToolOf({ current_tool: { tool: 'AskUserQuestion', questions: [{ text: 'Q1', options: [] }, { text: 'Q2', options: [] }] } });
    assert.equal(logic.activityOf({}, 'awaiting_permission', ask, labels).text, 'Q1 (+1)');
    const plan = logic.currentToolOf({ current_tool: { tool: 'ExitPlanMode', text: '' } });
    assert.equal(logic.activityOf({}, 'awaiting_permission', plan, labels).text, 'Plan to review');
    assert.deepEqual(logic.activityOf({ last_reply: '\n Done: all green.\nDetails' }, 'awaiting_input', null, labels),
        { kind: 'reply', text: 'Done: all green.', since: null });
    assert.equal(logic.activityOf({ last_reply: 'x' }, 'working', null, labels), null);
    assert.equal(logic.activityOf({ last_reply: 'x' }, 'errored', null, labels), null);
});

test('build session: a stopped or ended turn has no call in flight', () => {
    const raw = Object.assign({}, LIVE, {
        last_entry_kind: 'assistant_tool_use', pending_tool: true, last_tool_name: 'Bash', child_count: 1,
        current_tool: { tool: 'Bash', text: 'make' }, current_tool_age_seconds: 3,
    });
    assert.equal(logic.buildSession(raw, {}, null).current_tool.text, 'make');
    assert.equal(logic.buildSession(Object.assign({}, raw, { alive: false }), {}, null).current_tool, null);
    assert.equal(logic.buildSession(Object.assign({}, raw, { last_entry_kind: 'user_interrupt' }), {}, null).current_tool, null);
});

test('plan items and lines changed: malformed values show nothing', () => {
    assert.deepEqual(logic.planItemsOf({ plan_items: [
        { text: 'Read', status: 'completed' }, { text: 'Fix', status: 'weird' }, { text: '' }, null,
    ] }), [{ text: 'Read', status: 'completed' }, { text: 'Fix', status: 'pending' }]);
    assert.deepEqual(logic.linesOf({ lines_added: 12, lines_removed: 3 }), { added: 12, removed: 3 });
    assert.equal(logic.linesOf({ lines_added: 0, lines_removed: 0 }), null);
    assert.equal(logic.linesOf({ lines_added: 5, scan_pending: true }), null);
    assert.equal(logic.linesOf({ lines_added: '<b>', lines_removed: -1 }), null);
});

test('worked time: intervals of the last day, the open turn counted up to now', () => {
    const raw = Object.assign({}, LIVE, { work_intervals: [[3600, 3000], [1200, 600], 'x', [5, 10]], turn_open_seconds: 100 });
    const work = logic.workSummary(raw, 'working');
    assert.equal(work.work_seconds, 600 + 600 + 100);
    assert.equal(work.span_seconds, 3600);
    assert.equal(work.wait_seconds, 3600 - 1300);
    assert.equal(work.turns, 3);
    assert.deepEqual(work.segments[0], { left: 0, width: 600 / 3600 });
    // An ended session's span stops at its last turn, and an open turn no longer counts.
    const ended = logic.workSummary(Object.assign({}, raw, { alive: false }), 'completed');
    assert.equal(ended.span_seconds, 3600 - 600);
    assert.equal(ended.work_seconds, 1200);
    assert.equal(logic.workSummary({ work_intervals: [] }, 'completed'), null);
    // A turn waiting on a dialog counts up to its newest entry.
    const waiting = logic.workSummary(Object.assign({}, LIVE, { work_intervals: [], turn_open_seconds: 300, age_seconds: 120 }), 'awaiting_permission');
    assert.equal(waiting.work_seconds, 180);
    assert.equal(waiting.wait_seconds, 120);
    // A turn an earlier process left open counts only from this process's start.
    const reopened = logic.workSummary(Object.assign({}, LIVE, { work_intervals: [], turn_open_seconds: 72000, age_seconds: 2 }), 'working');
    assert.equal(reopened.work_seconds, 600);
});

test('project group order: most urgent status first, then newest activity, a missing age last', () => {
    const sessions = [
        { session_id: 'a', name: 'A', status: 'completed', age_seconds: 1 },
        { session_id: 'b', name: 'B', status: 'working', age_seconds: 50 },
        { session_id: 'c', name: 'C', status: 'working', age_seconds: null },
        { session_id: 'd', name: 'D', status: 'working', age_seconds: 5 },
        { session_id: 'e', name: 'E', status: 'awaiting_permission', age_seconds: 900 },
        { session_id: 'f', name: 'F', status: 'not-a-status', age_seconds: 0 },
    ];
    assert.deepEqual(logic.sortGroupSessions(sessions).map((session) => session.session_id), ['e', 'd', 'b', 'c', 'a', 'f']);
    assert.deepEqual(logic.sortGroupSessions(undefined), []);
});

test('tests label: failed first, a hostile count reads as zero', () => {
    const labels = { tests_passed: '{count} passed', tests_failed: '{count} failed', tests_skipped: '{count} skipped' };
    assert.deepEqual(logic.testsLabel({ passed: 42, failed: 0, skipped: 0 }, labels), { ok: true, text: '✓ 42 passed' });
    assert.deepEqual(logic.testsLabel({ passed: 39, failed: 3, skipped: 2 }, labels), { ok: false, text: '✗ 3 failed · 39 passed · 2 skipped' });
    assert.deepEqual(logic.testsLabel({ passed: '<b>', failed: -1 }, labels), { ok: true, text: '✓ 0 passed' });
    assert.equal(logic.testsLabel(null, labels), null);
});

test('graph model: subagents hang off their session, messages link sessions and peers', () => {
    const lead = { name: 'Lead', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working',
        agents: logic.agentsOf({ agents: [
            { id: 'a1', type: 'Explore', description: 'Find', running: true, age_seconds: 2, parent_id: '', workflow: '', started_seconds: 400 },
            { id: 'a2', type: 'Plan', description: '', running: false, age_seconds: 90, parent_id: 'a1', workflow: '', started_seconds: 300 },
            { id: 'w1', type: 'worker', running: true, workflow: 'run-1', started_seconds: 100 },
            { id: '', type: 'broken' },
        ] }),
        messages: logic.messagesOf({ messages: [
            { direction: 'sent', peer: 'Reviewer', age_seconds: 10 },
            { direction: 'received', peer: 'a1', label: 'Explore', handback: true, age_seconds: 300 },
            { direction: 'sent', peer: 'outside-team', age_seconds: 4000 },
            { direction: 'sideways', peer: 'x' },
        ] }) };
    const reviewer = { name: 'Reviewer', session_id: 'bbbbbbbb-1111-2222-3333-444444444444', status: 'awaiting_input', agents: [], messages: [] };
    const model = logic.graphModel([{ key: 'k1', session: lead }, { key: 'k2', session: reviewer }]);
    const ids = model.nodes.map((node) => node.id);
    assert.deepEqual(ids, ['s:k1', 's:k2', 's:k1/a:a1', 's:k1/a:a2', 's:k1/w:run-1', 's:k1/a:w1', 'p:outside-team']);
    const edge = (from, to, kind) => model.edges.find((item) => item.from === from && item.to === to && item.kind === kind);
    assert.equal(edge('s:k1', 's:k1/a:a1', 'spawn').active, true);
    assert.equal(edge('s:k1/a:a1', 's:k1/a:a2', 'spawn').active, false);
    assert.equal(edge('s:k1', 's:k1/w:run-1', 'spawn').active, true);
    assert.equal(edge('s:k1', 's:k2', 'message').active, true);
    // A subagent's hand-back belongs to the edge it already has to its caller.
    assert.equal(edge('s:k1/a:a1', 's:k1', 'message'), undefined);
    assert.equal(edge('s:k1', 's:k1/a:a1', 'spawn').report, 'completed');
    assert.equal(edge('s:k1', 'p:outside-team', 'message').active, false);

    const layout = logic.graphLayout(model);
    assert.equal(layout.nodes.length, model.nodes.length);
    assert.equal(layout.edges.length, model.edges.length);
    const at = new Map(layout.nodes.map((node) => [node.id, node]));
    assert.ok(at.get('s:k1/a:a2').x > at.get('s:k1/a:a1').x);
    assert.ok(at.get('s:k2').y > at.get('s:k1').y);
    for (const node of layout.nodes) {
        assert.ok(node.x + 1 <= layout.width && node.y + 1 <= layout.height);
    }
});

test('graph edges: what came back from a subagent is marked on its one edge', () => {
    const agent = (id, fields) => Object.assign({ id: id, running: false, age_seconds: 60, started_seconds: 600 }, fields);
    const lead = { name: 'Lead', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working',
        agents: logic.agentsOf({ agents: [
            agent('a1', { launch: 'foreground' }),
            agent('a2', { launch: 'background' }),
            agent('a3', { launch: 'background' }),
            agent('a4', { launch: 'background' }),
            agent('a5', { launch: 'background', running: true }),
            agent('a6', { launch: 'skill' }),
            agent('a7', { launch: 'background' }),
            agent('a8', { launch: 'background' }),
        ] }, {}),
        messages: logic.messagesOf({ messages: [
            { direction: 'received', peer: 'a2', handback: true, age_seconds: 30 },
            // The newest report decides: stopped, then resumed and done.
            { direction: 'received', peer: 'a3', handback: true, outcome: 'stopped', age_seconds: 50 },
            { direction: 'received', peer: 'a3', handback: true, outcome: 'completed', age_seconds: 20 },
            { direction: 'received', peer: 'a4', handback: true, outcome: 'completed', age_seconds: 50 },
            { direction: 'received', peer: 'a4', handback: true, outcome: 'killed', age_seconds: 20 },
            { direction: 'received', peer: 'a5', handback: true, age_seconds: 40 },
            { direction: 'sent', peer: 'a5', age_seconds: 5 },
            { direction: 'received', peer: 'a7', handback: true, outcome: 'constructor', age_seconds: 10 },
        ] }) };
    const model = logic.graphModel([{ key: 'k', session: lead }]);
    const spawn = (id) => model.edges.find((edge) => edge.to === 's:k/a:' + id);
    const marks = (id) => spawn(id).heads.map((head) => head.mark + ':' + head.slot);
    assert.deepEqual(model.edges.filter((edge) => edge.kind === 'message'), []);
    assert.deepEqual(marks('a1'), ['return:back']);
    assert.deepEqual(marks('a2'), ['return:back']);
    assert.deepEqual(marks('a3'), ['return:back']);
    assert.deepEqual(marks('a4'), ['abort:back']);
    // A running agent has reported nothing yet; a message sent to it points into its box.
    assert.deepEqual(marks('a5'), ['message:tip']);
    assert.deepEqual(marks('a6'), ['return:back']);
    // An outcome the UI does not know reads as a plain report.
    assert.deepEqual(marks('a7'), ['return:back']);
    // A background agent that never reported back has no mark.
    assert.deepEqual(marks('a8'), []);
    // The edge is as recent as its newest message.
    assert.equal(spawn('a5').age_seconds, 5);

    const labels = {
        graph_launch_background: 'Started in the background', graph_edge_returned: 'Reported back', graph_edge_aborted: 'Ended without a result',
        graph_edge_running: 'Running', graph_edge_no_return: 'No report back', graph_edge_messages_to: 'Messages to it: {count}',
        graph_edge_age: 'Last: {age}', age_seconds: '{s}s', age_minutes: '{m}m',
    };
    const names = new Map();
    assert.equal(logic.graphEdgeTip(spawn('a2'), labels, names), 'Started in the background\nReported back\nLast: 30s');
    assert.equal(logic.graphEdgeTip(spawn('a4'), labels, names), 'Started in the background\nEnded without a result\nLast: 20s');
    assert.equal(logic.graphEdgeTip(spawn('a5'), labels, names), 'Started in the background\nRunning\nMessages to it: 1');
    assert.equal(logic.graphEdgeTip(spawn('a8'), labels, names), 'Started in the background\nNo report back\nLast: 1m');

    // Placed: the report one head out from the agent's box, pointing back to the caller.
    const layout = logic.graphLayout(model);
    const box = layout.nodes.find((node) => node.id === 's:k/a:a2');
    const placed = layout.edges.find((edge) => edge.to === 's:k/a:a2');
    const y = box.y + 19;
    assert.equal(placed.marks[0].d, 'M' + (box.x - 16) + ' ' + y + ' L' + (box.x - 9) + ' ' + (y - 4) + ' L' + (box.x - 9) + ' ' + (y + 4) + ' Z');
    assert.ok(placed.path.endsWith(' H' + box.x));
    const html = logic.graphMarkup(layout, labels, null);
    assert.ok(html.includes('class="edge-mark mark-abort"'));
    assert.ok(html.includes('class="edge-hit"'));
});

test('graph edges: messages between two nodes share one edge, with an arrowhead at each receiver', () => {
    const one = { name: 'One', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working', agents: [],
        messages: logic.messagesOf({ messages: [
            { direction: 'sent', peer: 'Two', age_seconds: 4000 },
            { direction: 'received', peer: 'Two', age_seconds: 30 },
            { direction: 'sent', peer: 'Outside', age_seconds: 7200 },
        ] }) };
    const two = { name: 'Two', session_id: 'bbbbbbbb-1111-2222-3333-444444444444', status: 'working', agents: [],
        messages: logic.messagesOf({ messages: [{ direction: 'received', peer: 'One', age_seconds: 4000 }] }) };
    const model = logic.graphModel([{ key: '1', session: one }, { key: '2', session: two }]);
    const messages = model.edges.filter((edge) => edge.kind === 'message');
    assert.equal(messages.length, 2);
    const between = messages.find((edge) => edge.received.has('s:2'));
    assert.deepEqual(Array.from(between.received), [['s:2', 2], ['s:1', 1]]);
    assert.deepEqual(between.heads.map((head) => head.node).sort(), ['s:1', 's:2']);
    // The newest message ran to One, the top box, where the path starts: the dots run against it.
    assert.equal(between.flow, 's:1');
    assert.equal(between.active, true);
    const outside = messages.find((edge) => edge.to === 'p:outside');
    assert.deepEqual(outside.heads.map((head) => head.node), ['p:outside']);

    const layout = logic.graphLayout(model);
    const placed = layout.edges.find((edge) => edge.received && edge.received.has('s:2'));
    assert.equal(placed.reverse, true);
    assert.equal(placed.marks.length, 2);
    const labels = { graph_message_route: '{from} → {to}: {count}', graph_edge_age: 'Last: {age}', age_seconds: '{s}s', age_hours: '{h}h' };
    assert.equal(logic.graphEdgeTip(placed, labels, new Map([['s:1', 'One'], ['s:2', 'Two']])), 'One → Two: 2\nTwo → One: 1\nLast: 30s');
    const html = logic.graphMarkup(layout, labels, null);
    assert.ok(html.includes('keyPoints="1;0"'));
    // Quiet for more than an hour, the message to the outside fades.
    assert.ok(html.includes('class="edge edge-message stale"'));
    assert.ok(html.includes('class="edge-mark mark-message stale"'));
});

test('graph legend: every line, mark and box in the graph\'s own classes, labels kept as text', () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const keys = ['graph_legend_lines', 'graph_legend_boxes', 'graph_legend_spawn', 'graph_edge_running', 'graph_edge_returned', 'graph_edge_aborted',
        'graph_legend_message', 'graph_legend_tool', 'graph_legend_stale', 'graph_legend_session', 'graph_legend_box_running',
        'graph_legend_box_done', 'graph_legend_box_failed', 'graph_legend_box_ended', 'graph_legend_marks'];
    const html = logic.graphLegendMarkup(Object.fromEntries(keys.map((key) => [key, hostile])));
    assert.ok(!html.includes('<img'));
    assert.equal((html.match(/&lt;img src=x onerror=alert\(1\)&gt;/g) || []).length, keys.length);
    for (const part of ['edge edge-spawn active', 'edge edge-message', 'edge edge-tool', 'edge edge-spawn stale',
        'edge-mark mark-return', 'edge-mark mark-abort', 'edge-mark mark-message', 'gnode gnode-tool failed', 'gnode gnode-process ended']) {
        assert.ok(html.includes('class="' + part + '"'), part);
    }
    // One row each for the lines, the marks and the boxes.
    assert.equal((html.match(/class="legend-group"/g) || []).length, 3);
    assert.ok(!/style=|href|<script/.test(html));
    assert.ok(logic.graphLegendMarkup(null).startsWith('<div class="graph-legend">'));
});

test('graph markup: hostile names stay text, dots ride only on active edges', () => {
    const hostile = '"><img src=x onerror=alert(1)>';
    const session = { name: hostile, session_id: 'cccccccc-1111-2222-3333-444444444444', status: 'working',
        agents: [{ id: 'a"1', type: hostile, description: hostile, running: true, age_seconds: 1, parent_id: '', workflow: '' }], messages: [] };
    const layout = logic.graphLayout(logic.graphModel([{ key: 'k"<1', session: session }]));
    const html = logic.graphMarkup(layout, { graph_messages: '{count}', graph_workflow: 'Workflow', graph_peer: 'peer' }, 'k"<1');
    assert.ok(!html.includes('<img'));
    assert.ok(!html.includes('onerror=alert(1)>'));
    assert.ok(html.includes('data-key="k&quot;&lt;1"'));
    assert.ok(html.includes(' selected'));
    assert.equal((html.match(/<animateMotion/g) || []).length, 2);
    assert.ok(!/https?:/.test(html));
});

test('graph agents: a forked skill is titled by its skill, and every node says model, cost and age', () => {
    const rate = { input: 3, output: 15, cache_read: 0.3, cache_write_5m: 3.75, cache_write_1h: 6 };
    const prices = { 'sonnet-5-5': rate, 'opus-5-5': rate };
    const raw = { agents: [
        { id: 'a1', type: 'general-purpose', description: '', skill: 'commit', running: false, age_seconds: 7200, parent_id: '', workflow: '',
            usage_by_model: { 'claude-sonnet-5-5': { input_tokens: 1000, output_tokens: 2000 }, 'claude-opus-5-5': { output_tokens: 5 } } },
        { id: 'a2', type: 'Explore', description: 'Map the input', skill: '', running: true, age_seconds: 30, parent_id: '', workflow: '',
            usage_by_model: [] },
        { id: 'a3', type: 'general-purpose', running: false, age_seconds: 60, usage_by_model: 'nope' },
    ] };
    const agents = logic.agentsOf(raw, prices);
    assert.equal(agents[0].model, 'claude-sonnet-5-5');
    assert.equal(agents[1].model, null);
    assert.equal(agents[1].cost_usd, null);
    const session = { name: 'Lead', session_id: 'bbbbbbbb-1111-2222-3333-444444444444', status: 'working', agents: agents, messages: [], processes: 0 };
    // Without a known start the agents order by their last activity, oldest first.
    const nodes = logic.graphModel([{ key: 'k', session: session }]).nodes.filter((node) => node.kind === 'agent');
    assert.deepEqual(nodes.map((node) => [node.number, node.label]), [[1, '/commit'], [2, 'general-purpose'], [3, 'Map the input']]);
    const labels = { age_seconds: '{s}s', age_minutes: '{m}m', age_hours: '{h}h', age_days: '{d}d' };
    assert.deepEqual(logic.agentFacts(nodes[0], labels), ['Sonnet 5.5', '<$1', '2h']);
    assert.deepEqual(logic.agentFacts(nodes[2], labels), ['30s']);
    // The box carries the number, the title and the short facts; the tooltip
    // only completes the title, and the node names its session and agent for a click.
    const html = logic.graphMarkup(logic.graphLayout(logic.graphModel([{ key: 'k', session: session }])), labels, { key: 'k', agent: 'a1' });
    assert.ok(html.includes('data-tip="#1 /commit"'));
    assert.ok(html.includes('>Sonnet 5.5 · &lt;$1 · 2h<'));
    assert.ok(/class="gnode gnode-agent selected"[^>]*data-key="k" data-agent="a1"/.test(html));
    assert.ok(!/gnode-session[^"]*selected/.test(html));
});

test('graph agents: numbered in the order they started, with their runtime', () => {
    const agents = logic.agentsOf({ agents: [
        { id: 'late', running: true, age_seconds: 5, started_seconds: 60 },
        { id: 'early', running: false, age_seconds: 600, started_seconds: 3600 },
        { id: 'pending', running: false, age_seconds: 1800 },
        { id: 'twin-b', running: false, age_seconds: 10, started_seconds: 120 },
        { id: 'twin-a', running: false, age_seconds: 10, started_seconds: 120 },
    ] }, {});
    const ordered = logic.agentsByStart(agents);
    assert.deepEqual(ordered.map((agent) => [agent.number, agent.id]), [[1, 'early'], [2, 'pending'], [3, 'twin-a'], [4, 'twin-b'], [5, 'late']]);
    assert.equal(logic.agentRuntime(ordered[0]), 3000);
    assert.equal(logic.agentRuntime(ordered[4]), 60);
    assert.equal(logic.agentRuntime(ordered[1]), null);
    const launches = logic.agentsOf({ agents: [{ id: 'x', launch: 'constructor', effort: 'high', tool_calls: 4, lines_added: -2 }] }, {});
    assert.deepEqual([launches[0].launch, launches[0].effort, launches[0].tool_calls, launches[0].lines_added], ['', 'high', 4, 0]);
});

test('markdown: the structure Claude writes renders as its elements', () => {
    const md = logic.markdownToHtml([
        '# Title', '', 'Some **bold**, *it*, _it2_, ~~gone~~, snake_case_name and \u0060a|b\u0060.', 'same paragraph', '',
        '- one', '- two', '  - nested', '- [x] done', '- [ ] open', '', '3. third', '4. fourth', '',
        '> quoted', '', '| A | B |', '|---|--:|', '| 1 | 2 |', '', '\u0060\u0060\u0060js', 'const x = 1;', '\u0060\u0060\u0060', '', '---',
    ].join('\n'));
    assert.ok(md.startsWith('<h1>Title</h1><p>Some <strong>bold</strong>, <em>it</em>, <em>it2</em>, <del>gone</del>, snake_case_name and <code>a|b</code>.\nsame paragraph</p>'));
    assert.ok(md.includes('<ul><li><p>one</p></li><li><p>two</p><ul><li><p>nested</p></li></ul></li>'));
    assert.ok(md.includes('<li class="md-task"><span class="md-box">☑</span><p>done</p></li><li class="md-task"><span class="md-box">☐</span><p>open</p></li>'));
    assert.ok(md.includes('<ol start="3"><li><p>third</p></li><li><p>fourth</p></li></ol>'));
    assert.ok(md.includes('<blockquote><p>quoted</p></blockquote>'));
    assert.ok(md.includes('<th>A</th><th class="md-right">B</th>') && md.includes('<td>1</td><td class="md-right">2</td>'));
    // A pipe in inline code or escaped stays in its cell; an unclosed code span does not swallow the row.
    const table = logic.markdownToHtml(['| Flag | Meaning |', '|---|---|', '| \u0060a|b\u0060 | x \\| y |', '| \u0060open | z |'].join('\n'));
    assert.ok(table.includes('<td><code>a|b</code></td><td>x | y</td>'));
    assert.ok(table.includes('<td>\u0060open</td><td>z</td>'));
    assert.ok(md.includes('<pre class="md-code"><code>const x = 1;</code></pre><hr>'));
    assert.equal(logic.markdownToHtml(''), '');
    assert.equal(logic.markdownToHtml(null), '');
    // An unclosed fence runs to the end; a deep nest stays text past the limit.
    assert.equal(logic.markdownToHtml('\u0060\u0060\u0060\nopen'), '<pre class="md-code"><code>open</code></pre>');
    assert.ok(!logic.markdownToHtml('>'.repeat(40) + ' deep').includes('<blockquote><blockquote><blockquote><blockquote><blockquote><blockquote><blockquote><blockquote><blockquote><blockquote>'));
});

test('markdown: html safety - conversation text never becomes markup, a link never an href', () => {
    const hostile = [
        '<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '# <b onclick=x>head</b>',
        '[click](javascript:void0) and [x](https://a.b/"onmouseover=x)', '**<i>bold</i>**', '\u0060<code>\u0060',
        '| <b> | " |', '|---|---|', '| \' | & |', '- <u>item</u>', '> <svg onload=x>', '\u0060\u0060\u0060', '</code></pre><script>x</script>', '\u0060\u0060\u0060',
    ].join('\n');
    const html = logic.markdownToHtml(hostile);
    const allowed = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 'code', 'pre', 'span', 'ul', 'ol', 'li',
        'blockquote', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr']);
    for (const match of html.matchAll(/<\/?([a-zA-Z0-9]+)([^>]*)>/g)) {
        assert.ok(allowed.has(match[1]), 'unexpected element ' + match[1]);
        // Only the renderer's own attributes: a fixed class, a list start, a link's tooltip.
        for (const attribute of match[2].matchAll(/\s([a-zA-Z-]+)=/g)) {
            assert.ok(['class', 'start', 'data-tip'].includes(attribute[1]), 'unexpected attribute ' + attribute[1]);
        }
    }
    assert.ok(!html.includes('href'));
    assert.ok(!/<script|<img|<svg|<b |<i>|<u>/.test(html));
    assert.ok(html.includes('data-tip="javascript:void0"') && html.includes('data-tip="https://a.b/&quot;onmouseover=x"'));
    assert.ok(html.includes('&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;'));
});

test('graph fold: a collapsed session keeps its subtree folded away and says how much', () => {
    const lead = { name: 'Lead', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working', processes: 1, tools: [],
        process_list: [{ pid: 7, parent: 0, name: 'make' }],
        agents: logic.agentsOf({ agents: [{ id: 'a1b2c3d4e5f6a7b8c', running: true, age_seconds: 5 }, { id: 'a2', running: false, age_seconds: 50 }] }, {}),
        messages: logic.messagesOf({ messages: [{ direction: 'sent', peer: 'Reviewer', age_seconds: 10 }, { direction: 'sent', peer: 'a1b2c3d4e5f6a7b8c', age_seconds: 10 }] }) };
    const reviewer = { name: 'Reviewer', session_id: 'bbbbbbbb-1111-2222-3333-444444444444', status: 'awaiting_input', agents: [], messages: [], processes: 0 };
    const entries = [{ key: 'k1', session: lead }, { key: 'k2', session: reviewer }];

    const open = logic.graphModel(entries, null, null, new Set());
    const folded = logic.graphModel(entries, null, null, new Set([lead.session_id]));

    assert.equal(open.nodes.length, 5);
    assert.deepEqual(folded.nodes.map((node) => node.id), ['s:k1', 's:k2']);
    const session = folded.nodes[0];
    assert.deepEqual([session.collapsed, session.below, session.sessionId], [true, 3, lead.session_id]);
    // A message between sessions stays; one to a folded-away subagent goes with it.
    assert.deepEqual(folded.edges.map((edge) => edge.from + '>' + edge.to), ['s:k1>s:k2']);

    const labels = { graph_hidden: '{count} folded away' };
    const html = logic.graphMarkup(logic.graphLayout(folded), labels, null);
    assert.ok(html.includes('>3 folded away<'));
    assert.ok(html.includes('data-toggle="' + lead.session_id + '"') && html.includes('▸'));
    assert.ok(logic.graphMarkup(logic.graphLayout(open), labels, null).includes('▾'));
    // A session with nothing below it has no toggle.
    assert.ok(!html.includes('data-toggle="' + reviewer.session_id + '"'));
});

test('graph window: old agents drop out, running ones and the parents of kept ones stay', () => {
    const agents = [
        { id: 'a-old', running: false, age_seconds: 90000, parent_id: '' },
        { id: 'a-parent', running: false, age_seconds: 200000, parent_id: '' },
        { id: 'a-child', running: false, age_seconds: 600, parent_id: 'a-parent' },
        { id: 'a-busy', running: true, age_seconds: 500000, parent_id: '' },
        { id: 'a-unknown', running: false, age_seconds: null, parent_id: '' },
    ];
    assert.deepEqual(logic.agentsInWindow(agents, 86400).map((agent) => agent.id), ['a-parent', 'a-child', 'a-busy']);
    assert.equal(logic.agentsInWindow(agents, null).length, 5);
});

test('graph window: messages outside it draw no edge, and an unknown range key falls back to 24 h', () => {
    const lead = { name: 'Lead', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working', agents: [], processes: 0,
        messages: logic.messagesOf({ messages: [
            { direction: 'sent', peer: 'Reviewer', age_seconds: 4000 },
            { direction: 'sent', peer: 'Archivist', age_seconds: 400000 },
        ] }) };
    const kinds = (seconds) => logic.graphModel([{ key: 'k', session: lead }], seconds).nodes.map((node) => node.label);
    assert.deepEqual(kinds(logic.graphRange('24h').seconds), ['Lead', 'Reviewer']);
    assert.deepEqual(kinds(logic.graphRange('1h').seconds), ['Lead']);
    assert.deepEqual(kinds(logic.graphRange('30d').seconds), ['Lead', 'Reviewer', 'Archivist']);
    assert.equal(logic.graphRange('constructor').key, logic.DEFAULT_GRAPH_RANGE);
    assert.deepEqual(kinds(logic.graphRange('all').seconds), ['Lead', 'Reviewer', 'Archivist']);
});

test('finiteOrNull: a number or a numeric string, never a blank read as zero', () => {
    assert.deepEqual([5, 0, -2.5, '7', ' 8 ', '0'].map(logic.finiteOrNull), [5, 0, -2.5, 7, 8, 0]);
    assert.deepEqual(['', '  ', [], [3], {}, true, null, undefined, NaN, Infinity, 'x'].map(logic.finiteOrNull),
        [null, null, null, null, null, null, null, null, null, null, null]);
});

test('agent steps: calls and replies in order, each call named by what it acts on', () => {
    const steps = logic.agentStepsOf([
        { kind: 'text', text: 'Looking.', at: 100 },
        { kind: 'call', id: 'u1', tool: 'Bash', input: { command: 'npm test\nmore', description: 'Run the tests' }, result: 'ok', error: false, at: 101, result_at: 113.5 },
        { kind: 'call', id: 'u2', tool: 'Read', input: { file_path: 'a.txt' }, result: null, at: 120 },
        { kind: 'call', id: 'u3', tool: 'X', input: [1], result: 5, error: 'yes', outcome: 'constructor', exit_code: '1' },
        { kind: 'call', id: 'u4', tool: 'Bash', input: {}, result: 'Exit code 2', error: true, outcome: 'failed', exit_code: 2 },
        { kind: 'call', id: '', tool: 'Bad' }, { kind: 'other' }, null,
    ]);
    assert.deepEqual(steps.map((step) => step.kind + ':' + (step.tool || step.text)), ['text:Looking.', 'call:Bash', 'call:Read', 'call:X', 'call:Bash']);
    assert.deepEqual([steps[3].input, steps[3].result, steps[3].error, steps[3].outcome, steps[3].exit_code], [{}, null, false, '', null]);
    assert.deepEqual([steps[4].outcome, steps[4].exit_code], ['failed', 2]);
    assert.equal(logic.stepSubject(steps[1]), 'Run the tests');
    assert.equal(logic.stepSubject({ input: { command: 'npm test\nmore' } }), 'npm test');
    assert.equal(logic.stepSubject({ input: { limit: 3, target: 'chan' } }), 'chan');
    assert.equal(logic.stepSubject({ input: { constructor: 'x' } }), 'x');
    assert.equal(logic.stepInputText({ a: 1 }), '{\n  "a": 1\n}');
    assert.deepEqual([logic.formatClock(5), logic.formatClock(754), logic.formatClock(3723)], ['0:05', '12:34', '1:02:03']);
    assert.deepEqual([logic.stepDuration(0.42), logic.stepDuration(12.5)], ['0.4s', '13s']);
});

test('graph tools: each caller has its own tool nodes, drawn for what is in focus', () => {
    const session = { name: 'Lead', session_id: 'aaaaaaaa-1111-2222-3333-444444444444', status: 'working', messages: [], processes: 0,
        tools: logic.toolsOf([
            { name: 'Bash', calls: 12, age_seconds: 30, failed: 2 }, { name: 'mcp__discord__read_messages', calls: 3, age_seconds: 4000, failed: -1 },
            { name: 'mcp__discord__send', calls: 2, age_seconds: 5000 }, { name: 'Agent', calls: 2, age_seconds: 30 },
            { name: 'Old', calls: 1, age_seconds: 200000 }, { name: '', calls: 4 }, { name: 'Zero', calls: 0 },
        ]),
        agents: logic.agentsOf({ agents: [{ id: 'a1', type: 'Explore', running: false, age_seconds: 100, started_seconds: 200,
            tools: [{ name: 'Bash', calls: 5, age_seconds: 100 }, { name: 'SubagentHandback', calls: 1, age_seconds: 100 }] }] }, {}) };
    const day = logic.graphRange('24h').seconds;
    const toolNodes = (focus) => logic.graphModel([{ key: 'k', session: session }], day, focus).nodes
        .filter((node) => node.kind === 'tool').map((node) => [node.id, node.parent, node.calls]);

    // Nothing selected, a process, or another session: no tools at all.
    assert.deepEqual(toolNodes(null), []);
    assert.deepEqual(toolNodes({ key: 'k', process: 400 }), []);
    assert.deepEqual(toolNodes({ key: 'other' }), []);
    // A subagent: its own tools under it.
    assert.deepEqual(toolNodes({ key: 'k', agent: 'a1' }), [['s:k/a:a1/t:Bash', 's:k/a:a1', 5]]);
    // A session: its own tools (an MCP server's folded into one) and its subagents'.
    assert.deepEqual(toolNodes({ key: 'k' }), [
        ['s:k/a:a1/t:Bash', 's:k/a:a1', 5], ['s:k/t:Bash', 's:k', 12], ['s:k/t:mcp:discord', 's:k', 5],
    ]);

    const model = logic.graphModel([{ key: 'k', session: session }], day, { key: 'k' });
    const edge = model.edges.find((item) => item.to === 's:k/t:Bash');
    assert.deepEqual([edge.from, edge.kind, edge.count, edge.active], ['s:k', 'tool', 12, true]);
    const labels = { graph_tool_calls: 'Calls: {count}', graph_mcp: 'MCP server' };
    const html = logic.graphMarkup(logic.graphLayout(model), labels, { key: 'k', agent: 'a1', tool: 'Bash' });
    assert.ok(/class="gnode gnode-tool selected"[^>]*data-key="k" data-agent="a1" data-tool="Bash"/.test(html));
    assert.ok(html.includes('>MCP server · Calls: 5<'));
    // A tool with failed calls says how many, and is marked.
    const failing = logic.graphMarkup(logic.graphLayout(model), Object.assign({ graph_tool_failed: 'Failed: {count}' }, labels), null);
    assert.ok(failing.includes('>Calls: 12 · Failed: 2<'));
    assert.ok(/class="gnode gnode-tool running failed"|class="gnode gnode-tool failed"/.test(failing));
    assert.ok(!/gnode-agent[^"]*selected/.test(html));
    assert.equal(logic.toolNodeKey('mcp__a__b__c'), 'mcp:a');
});

test('graph processes: each process is a node under the one that started it, and selectable', () => {
    const processes = logic.processesOf({ child_processes: [
        { pid: 300, parent: 0, name: 'bash' }, { pid: 400, parent: 300, name: 'python' }, { pid: 500, parent: 400, name: 'tool' },
        { pid: 600, parent: 999, name: 'orphan' }, { pid: -1, parent: 0, name: 'bad' }, { pid: 7, name: 7 }, null,
    ] });
    assert.deepEqual(processes.map((process) => [process.pid, process.parent]), [[300, 0], [400, 300], [500, 400], [600, 999]]);
    const session = { name: 'Build', session_id: 'eeeeeeee-1111-2222-3333-444444444444', status: 'working', agents: [], messages: [],
        processes: 4, process_list: processes };
    const model = logic.graphModel([{ key: 'k', session: session }]);
    const parentOf = new Map(model.nodes.map((node) => [node.id, node.parent]));
    assert.equal(parentOf.get('s:k/p:300'), 's:k');
    assert.equal(parentOf.get('s:k/p:400'), 's:k/p:300');
    assert.equal(parentOf.get('s:k/p:500'), 's:k/p:400');
    assert.equal(parentOf.get('s:k/p:600'), 's:k');
    assert.ok(!model.nodes.some((node) => node.id === 's:k/p'));

    const html = logic.graphMarkup(logic.graphLayout(model), { fact_pid: 'PID' }, { key: 'k', agent: null, process: 400 });
    assert.ok(html.includes('>python<') && html.includes('>PID 400<'));
    assert.ok(/class="gnode gnode-process running selected"[^>]*data-key="k" data-process="400"/.test(html));
    assert.ok(!/gnode-session[^"]*selected/.test(html));
});

test('ended processes: kept dimmed for a minute, longer while selected, and live again when listed again', () => {
    const bash = { pid: 300, parent: 0, name: 'bash' };
    const python = { pid: 400, parent: 300, name: 'python' };
    let tracker = logic.trackEndedProcesses(null, [{ key: 'k', processes: [bash, python] }], 1000);
    assert.equal(tracker.ended.size, 0);
    // Both gone: both ended now, with their tree kept.
    tracker = logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [] }], 2000);
    const fadeAt = 2000 + logic.PROCESS_LINGER_SECONDS * 1000;
    assert.deepEqual(tracker.ended.get('k'), [
        { pid: 300, parent: 0, name: 'bash', ended_at: 2000, fade_at: fadeAt },
        { pid: 400, parent: 300, name: 'python', ended_at: 2000, fade_at: fadeAt },
    ]);
    // A re-render of the same snapshot changes nothing.
    assert.deepEqual(logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [] }], 2500).ended.get('k').map((p) => p.ended_at), [2000, 2000]);
    // The pid listed again is live, not ended.
    const reused = logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [{ pid: 300, parent: 0, name: 'node' }] }], 3000);
    assert.deepEqual(reused.ended.get('k').map((p) => p.pid), [400]);
    // While it fades it is still there; once faded only the selected one stays.
    const fading = fadeAt + 1000;
    assert.deepEqual(logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [] }], fading).ended.get('k').map((p) => p.pid), [300, 400]);
    const late = fadeAt + logic.PROCESS_FADE_SECONDS * 1000 + 1;
    const kept = logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [] }], late, (key, pid) => key === 'k' && pid === 400);
    assert.deepEqual(kept.ended.get('k').map((p) => p.pid), [400]);
    assert.equal(logic.trackEndedProcesses(tracker, [{ key: 'k', processes: [] }], late).ended.size, 0);
    // The selected one fades in full once let go: its fade stays a fade's length ahead.
    assert.equal(kept.ended.get('k')[0].fade_at, late + logic.PROCESS_FADE_SECONDS * 1000);
    const letGo = logic.trackEndedProcesses(kept, [{ key: 'k', processes: [] }], late + 3000);
    assert.deepEqual(letGo.ended.get('k').map((p) => p.pid), [400]);
    // A session no longer listed takes its processes along.
    assert.equal(logic.trackEndedProcesses(tracker, [], 2500).ended.size, 0);
});

test('graph processes: an ended process stays under its parent, dimmed and selectable', () => {
    const session = { name: 'Build', session_id: 'eeeeeeee-1111-2222-3333-444444444444', status: 'working', agents: [], messages: [],
        processes: 1, process_list: [{ pid: 300, parent: 0, name: 'bash' }] };
    const ended = [{ pid: 400, parent: 300, name: 'python', ended_at: 1, fade_in: -2 }];
    const model = logic.graphModel([{ key: 'k', session: session, ended: ended }]);
    const node = model.nodes.find((item) => item.id === 's:k/p:400');
    assert.equal(node.parent, 's:k/p:300');
    assert.equal(node.running, false);
    assert.equal(node.ended, true);
    assert.deepEqual(model.edges.filter((edge) => edge.to === 's:k/p:400').map((edge) => [edge.kind, edge.active]), [['ended', false]]);

    assert.equal(node.fade_in, -2);
    const html = logic.graphMarkup(logic.graphLayout(model), { fact_pid: 'PID', process_ended: 'Ended' }, { key: 'k', agent: null, process: 400 });
    assert.ok(html.includes('>Ended · PID 400<'));
    assert.ok(/class="gnode gnode-process ended selected"[^>]*data-key="k" data-process="400"/.test(html));
    // The edge to the selected process is drawn stronger.
    assert.ok(html.includes('edge edge-ended linked"'));
    // While it is selected neither the node nor its edge fades, however long no render comes.
    assert.ok(!html.includes('attributeName="stroke-opacity"'));
    assert.ok(!html.includes('attributeName="opacity"'));
    // Let go, both fade from wherever the fade stood.
    const unselected = logic.graphMarkup(logic.graphLayout(model), { fact_pid: 'PID', process_ended: 'Ended' }, { key: 'k', agent: null });
    assert.ok(unselected.includes('<animate attributeName="stroke-opacity" from="1" to="0" begin="-2s" dur="5s" fill="freeze"></animate></path>'));
    assert.ok(/data-process="400"[^>]*><g><animate attributeName="opacity" from="1" to="0" begin="-2s" dur="5s" fill="freeze"><\/animate><rect/.test(unselected));
    // A running process never fades.
    assert.equal((unselected.match(/<animate attributeName="opacity"/g) || []).length, 1);
});

test('gitSignal: what is not finished, in one signal ranked first', () => {
    const labels = {
        git_uncommitted: 'Uncommitted: {count}', git_unpushed: 'Not pushed: {count}', git_local_only: 'Only local: {count}',
        git_merged: 'Merged', git_gone: 'Deleted on origin', git_tip_branch: 'Branch: {branch}',
        git_tip_files: 'Changed: {changed} · new: {untracked}', git_tip_unmerged: 'Commits not in {branch}: {count}',
        git_tip_squashed: 'In {branch} (squash)', git_tip_fetched: 'Fetched {age} ago', git_tip_never_fetched: 'Never fetched',
        git_tip_shared: 'Whole directory',
        signal_processes: 'Processes: {count}', signal_budget: 'Budget {percent}%', age_minutes: '{m}m',
    };
    const base = { branch: 'feature', detached: false, upstream: 'origin/feature', upstream_gone: false, ahead: 0, behind: 0,
        on_remote: true, default_branch: 'main', unmerged: 2, changes_in_default: null, changed: 0, untracked: 0, fetched_seconds: 120 };
    const signal = (overrides) => logic.gitSignal(Object.assign({}, base, overrides), labels);

    // Everything committed and pushed: nothing to say.
    assert.equal(signal({}), null);
    assert.equal(logic.gitSignal(null, labels), null);

    const open = signal({ changed: 2, untracked: 1, ahead: 3 });
    assert.equal(open.text, 'Uncommitted: 3 · Not pushed: 3');
    assert.equal(open.tone, 'warn');
    assert.equal(open.tip, 'Branch: feature → origin/feature\nChanged: 2 · new: 1\nWhole directory\nCommits not in main: 2\nFetched 2m ago');

    // Never pushed, and the commits are in no default branch.
    assert.equal(signal({ upstream: '', on_remote: false, ahead: null }).text, 'Only local: 2');
    // A fresh worktree branch with nothing of its own says nothing.
    assert.equal(signal({ upstream: '', on_remote: false, ahead: null, unmerged: 0 }), null);

    // Deleted on the remote: merged when its changes are in the default branch, a warning when not.
    const merged = signal({ on_remote: false, upstream_gone: true, ahead: null, changes_in_default: true });
    assert.deepEqual([merged.text, merged.tone], ['Merged', 'plain']);
    assert.ok(merged.tip.includes('In main (squash)'));
    const gone = signal({ on_remote: false, upstream_gone: true, ahead: null, changes_in_default: false, fetched_seconds: null });
    assert.deepEqual([gone.text, gone.tone], ['Deleted on origin', 'warn']);
    assert.ok(gone.tip.endsWith('Never fetched'));

    // The default branch itself is never "only local" or "merged".
    assert.equal(signal({ branch: 'main', upstream: '', on_remote: false, unmerged: null }), null);
    // A remote without a known default branch: nothing to count a local branch against, files still count.
    const noDefault = { default_branch: '', unmerged: null, upstream: '', on_remote: false, ahead: null };
    assert.equal(signal(noDefault), null);
    const dirtyNoDefault = signal(Object.assign({}, noDefault, { changed: 1 }));
    assert.equal(dirtyNoDefault.text, 'Uncommitted: 1');
    assert.ok(!dirtyNoDefault.tip.includes('Commits not in'));

    // Ranked first, ahead of running work and the budget warning.
    const session = { processes: 2, budget: { ratio: 0.9 }, git: Object.assign({}, base, { changed: 1 }) };
    assert.deepEqual(logic.sessionSignals(session, labels).map((item) => item.kind), ['git', 'processes', 'budget']);

    // The key is one per root and directory, the worktree first.
    assert.equal(logic.gitDirKey({ origin: 'wsl:U', cwd: '/a', worktree_cwd: '/a/wt' }), 'wsl:U\n/a/wt');
    assert.equal(logic.gitDirKey({ cwd: '' }), '');
});

test('gitSignal: html safety - a branch name stays text in the tooltip attribute', () => {
    const labels = { git_uncommitted: 'Uncommitted: {count}', git_tip_branch: 'Branch: {branch}', git_tip_never_fetched: 'Never fetched' };
    const hostile = logic.gitSignal({ branch: '"><img src=x onerror=alert(1)>', changed: 1, untracked: 0, on_remote: true }, labels);
    const markup = '<span' + logic.attr('data-tip', hostile.tip) + '>' + logic.esc(hostile.text) + '</span>';
    assert.ok(!markup.includes('<img'));
    assert.ok(markup.includes('&quot;&gt;&lt;img'));
});

test('media: listed images are checked, captioned by where they came from, and only real base64 becomes an address', () => {
    const items = logic.mediaItemsOf({ images: [
        { id: '120.0', media_type: 'image/png', at: 1700000000, origin: 'tool', tool: 'Read', path: 'C:\\docs\\shot.png', bytes: 2048 },
        { id: '80.1', media_type: 'image/jpeg', at: null, origin: 'pasted', tool: '', path: '', bytes: 10 },
        { id: '60.0', media_type: 'image/webp', origin: 'tool', tool: 'mcp__browser__browser_take_screenshot' },
        { id: '40.0', media_type: 'image/png', origin: 'tool', tool: 'mcp__figma__export_frame' },
        { id: '../etc', media_type: 'image/png' }, { id: '1.0', media_type: 'image/svg+xml' }, null, 'x',
    ] });
    assert.deepEqual(items.map((item) => item.id), ['120.0', '80.1', '60.0', '40.0']);
    assert.equal(logic.mediaItemsOf(null).length, 0);

    const labels = { media_pasted: 'Pasted by you', media_read: 'Read: {name}', media_screenshot: 'Browser screenshot' };
    assert.deepEqual(items.map((item) => logic.mediaCaption(item, labels)), ['Read: shot.png', 'Pasted by you', 'Browser screenshot', 'export_frame']);

    assert.equal(logic.mediaDataUrl('image/png', 'iVBORw0KGgo='), 'data:image/png;base64,iVBORw0KGgo=');
    assert.equal(logic.mediaDataUrl('image/svg+xml', 'PHN2Zz4='), '');
    assert.equal(logic.mediaDataUrl('image/png', '"><img src=x onerror=alert(1)>'), '');
    assert.equal(logic.mediaDataUrl('image/png', null), '');
});

test('branchWebUrl: the forge from the host or a pull request on it, each branch segment encoded, else no link', () => {
    assert.equal(logic.branchWebUrl('https://github.com/o/r', 'feat/x y'), 'https://github.com/o/r/tree/feat/x%20y');
    assert.equal(logic.branchWebUrl('https://gitlab.example.com/team.sub/repo', 'bug/ABC/Fix'), 'https://gitlab.example.com/team.sub/repo/-/tree/bug/ABC/Fix');
    assert.equal(logic.branchWebUrl('https://bitbucket.org/o/r', 'main'), 'https://bitbucket.org/o/r/src/main');
    assert.equal(logic.branchWebUrl('https://codeberg.org/o/r', 'main'), 'https://codeberg.org/o/r/src/branch/main');
    // A host that does not say what it is: a merge request on it does.
    assert.equal(logic.branchWebUrl('https://code.example.com/o/r', 'main', ['https://code.example.com/o/r/-/merge_requests/4']),
        'https://code.example.com/o/r/-/tree/main');
    assert.equal(logic.branchWebUrl('https://code.example.com/o/r', 'main', ['https://code.example.com/o/r/pull-requests/4']),
        'https://code.example.com/o/r/src/main');
    assert.equal(logic.branchWebUrl('https://code.example.com/o/r', 'main', ['https://code.example.com/o/r/pull/4']),
        'https://code.example.com/o/r/tree/main');
    assert.equal(logic.branchWebUrl('https://code.example.com/o/r', 'main', ['https://other.example.com/o/r/pull/4']), '');
    assert.equal(logic.branchWebUrl('https://code.example.com/o/r', 'main'), '');
    // html safety: a branch cannot leave its path segment, an odd address gives no link at all.
    assert.equal(logic.branchWebUrl('https://github.com/o/r', '"><img src=x>'), 'https://github.com/o/r/tree/%22%3E%3Cimg%20src%3Dx%3E');
    assert.equal(logic.branchWebUrl('javascript:alert(1)//github.com/o/r', 'main'), '');
    assert.equal(logic.branchWebUrl('https://github.com/o/r?x=1', 'main'), '');
    assert.equal(logic.branchWebUrl('https://github.com/o/r', ''), '');
});

test('commandLineText: arguments as one line, quoted only where a space, a tab or a quote needs it', () => {
    assert.equal(logic.commandLineText(['cmake', '--build', 'build']), 'cmake --build build');
    assert.equal(logic.commandLineText(['C:\\Program Files\\CMake\\cmake.exe', '-S', '.']), '"C:\\Program Files\\CMake\\cmake.exe" -S .');
    assert.equal(logic.commandLineText(['bash', '-c', 'echo "hi"']), 'bash -c "echo \\"hi\\""');
    // An empty argument stays visible; a trailing backslash cannot escape the closing quote.
    assert.equal(logic.commandLineText(['run', '']), 'run ""');
    assert.equal(logic.commandLineText(['cd', 'C:\\dir with space\\']), 'cd "C:\\dir with space\\\\"');
    assert.equal(logic.commandLineText(null), null);
    assert.equal(logic.commandLineText([]), '');
});

test('graph model: a session with background processes gets one running process node', () => {
    const session = { name: 'Build', session_id: 'eeeeeeee-1111-2222-3333-444444444444', status: 'working', agents: [], messages: [], processes: 3 };
    const idle = { name: 'Idle', session_id: 'ffffffff-1111-2222-3333-444444444444', status: 'awaiting_input', agents: [], messages: [], processes: 0 };
    const model = logic.graphModel([{ key: 'k', session: session }, { key: 'i', session: idle }]);
    assert.deepEqual(model.nodes.map((node) => [node.id, node.kind]), [['s:k', 'session'], ['s:i', 'session'], ['s:k/p', 'process']]);
    assert.deepEqual(model.edges.map((edge) => [edge.from, edge.to, edge.active]), [['s:k', 's:k/p', true]]);
    const html = logic.graphMarkup(logic.graphLayout(model), { signal_processes: 'Processes: {count}' }, null);
    assert.ok(html.includes('>Processes: 3<'));
    assert.ok(html.includes('gnode gnode-process running'));
});

test('graph model: a message to or from a subagent no longer listed draws nothing', () => {
    const session = { name: 'Lead', session_id: 'dddddddd-1111-2222-3333-444444444444', status: 'working', agents: [],
        messages: logic.messagesOf({ messages: [
            { direction: 'sent', peer: 'a8a4b323d80bdba84', age_seconds: 5 },
            { direction: 'received', peer: 'a858d0a217c7fa5da', label: 'Explore', handback: true, age_seconds: 5 },
        ] }) };
    const model = logic.graphModel([{ key: 'k', session: session }]);
    assert.deepEqual(model.nodes.map((node) => node.id), ['s:k']);
    assert.deepEqual(model.edges, []);
});
