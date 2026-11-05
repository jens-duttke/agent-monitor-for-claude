'use strict';

/* --- processes and task output: a block of the detail pane ---

   While the selected session runs background processes, the detail pane shows
   a live per-process table (CPU / memory / uptime, polled once a second) and the
   session's background tasks. A task row expands to a mono-space console that
   tails that task's live output file once a second while it is open. This is
   the one place the UI shows process output text; it is fetched on demand
   (read_task_output bridge), never on the snapshot poll, and only while a row is
   expanded. */
const PROC_POLL_MS = 1000;
const TASK_LIST_REFRESH_TICKS = 3;   // re-list tasks every 3rd stats tick, not every tick
const TASK_ACTIVE_SECONDS = 5;       // "active" when its output changed this recently
const TASK_AGE_GRACE_SECONDS = 60;   // keep tasks up to this much older than the oldest process

let procBlock = null;

// Show the processes and tasks of `session` in `el` (the block's body) and its
// process count in `noteEl` (the block's header). The same session keeps its
// running poll and its expanded consoles; another one starts afresh.
function showProcBlock(el, noteEl, session) {
    const key = rowKey(session);
    if (procBlock && procBlock.key === key && procBlock.el === el) {
        return;
    }
    stopProcBlock();

    const labels = state.labels;
    el.innerHTML = ''
        + '<table class="proc-table">'
        +     '<thead><tr>'
        +         '<th class="proc-name"></th><th class="proc-num"></th>'
        +         '<th class="proc-num"></th><th class="proc-num"></th>'
        +     '</tr></thead>'
        +     '<tbody></tbody>'
        + '</table>'
        + '<div class="block-sub"></div>'
        + '<div class="proc-task-list"></div>'
        + '<div class="proc-task-note" hidden></div>';
    const headCells = el.querySelectorAll('.proc-table thead th');
    headCells[0].textContent = labels.proc_col_process || '';
    headCells[1].textContent = labels.proc_col_cpu || '';
    headCells[2].textContent = labels.proc_col_memory || '';
    headCells[3].textContent = labels.proc_col_uptime || '';
    el.querySelector('.block-sub').textContent = labels.proc_tasks_title || '';

    procBlock = {
        el,
        noteEl,
        key,
        pid: Number(session.pid),
        sessionId: session.session_id || '',
        cwd: session.cwd || '',
        origin: session.origin || 'local',
        expanded: new Set(),
        tick: 0,
        timer: null,
    };
    procBlockLoop(procBlock);
}

function stopProcBlock() {
    if (!procBlock) {
        return;
    }
    clearTimeout(procBlock.timer);
    procBlock = null;
}

// Self-scheduling poll so a slow tick never overlaps the next. Fills the block
// immediately, then again every PROC_POLL_MS while it stays on screen.
async function procBlockLoop(panel) {
    if (procBlock !== panel) {
        return;
    }
    await refreshProcBlock(panel);
    if (procBlock !== panel) {
        return;
    }
    panel.timer = setTimeout(() => procBlockLoop(panel), PROC_POLL_MS);
}

async function refreshProcBlock(panel) {
    panel.tick += 1;

    const stats = await callProcessStats(panel.pid, panel.origin);
    if (procBlock !== panel) {
        return;
    }
    renderProcTable(panel, stats);
    panel.maxProcUptime = maxProcessUptime(stats);

    // The task list changes rarely, so re-list it only occasionally; the output
    // of expanded rows is what needs the per-second refresh.
    if (panel.tick === 1 || panel.tick % TASK_LIST_REFRESH_TICKS === 1) {
        // A task last written before the oldest running process started belongs
        // to an earlier run and is irrelevant, so it is dropped (small grace to
        // keep a just-started task that has not written yet).
        const cutoff = panel.maxProcUptime > 0 ? panel.maxProcUptime + TASK_AGE_GRACE_SECONDS : 0;
        const result = await callTasks(panel.sessionId, panel.cwd, cutoff, panel.origin);
        if (procBlock !== panel) {
            return;
        }
        renderTaskList(panel, result);
    }

    for (const taskId of Array.from(panel.expanded)) {
        const text = await callTaskOutput(panel.sessionId, panel.cwd, taskId, panel.origin);
        if (procBlock !== panel) {
            return;
        }
        setTaskOutput(panel, taskId, text);
    }
}

function renderProcTable(panel, stats) {
    const labels = state.labels;
    // The WSL2-VM row is context, not one of the session's processes, so the
    // count reflects only the real descendant processes (matching the badge).
    const processCount = stats.filter((stat) => stat.kind !== 'wsl_vm').length;
    if (panel.noteEl) {
        panel.noteEl.textContent = fmt(labels.processes_running, { count: processCount });
    }
    const tbody = panel.el.querySelector('.proc-table tbody');
    if (!tbody) {
        return;
    }
    if (stats.length === 0) {
        tbody.innerHTML = '<tr class="proc-empty"><td colspan="4"></td></tr>';
        tbody.querySelector('td').textContent = labels.proc_none || '';
        return;
    }
    let rows = '';
    for (const stat of stats) {
        if (stat.kind === 'wsl_vm') {
            rows += '<tr class="proc-vm">'
                + '<td class="proc-name"' + attr('data-tip', labels.proc_wsl_vm_tip || '') + '>' + esc(labels.proc_wsl_vm || 'WSL2 VM') + '</td>'
                + '<td class="proc-num">' + esc(formatCpu(stat.cpu)) + '</td>'
                + '<td class="proc-num">' + esc(formatBytes(stat.rss)) + '</td>'
                + '<td class="proc-num">-</td>'
                + '</tr>';
        } else {
            rows += '<tr>'
                + '<td class="proc-name">' + esc(stat.name) + '</td>'
                + '<td class="proc-num">' + esc(formatCpu(stat.cpu)) + '</td>'
                + '<td class="proc-num">' + esc(formatBytes(stat.rss)) + '</td>'
                + '<td class="proc-num">' + esc(formatDurationShort(stat.uptime)) + '</td>'
                + '</tr>';
        }
    }
    tbody.innerHTML = rows;
}

// Keyed reconcile (not innerHTML) so an expanded console and its scroll position
// survive a re-list.
function renderTaskList(panel, result) {
    const labels = state.labels;
    const listEl = panel.el.querySelector('.proc-task-list');
    const section = panel.el;
    const noteEl = panel.el.querySelector('.proc-task-note');
    const tasks = (result && result.tasks) || [];
    const total = (result && result.total) || tasks.length;

    // Keep the section visible even with no tasks, with an explanatory note -
    // this is where a user looks for process output, so silence would just read
    // as "broken". Only run_in_background tasks write a live output file.
    let emptyNote = section.querySelector('.proc-task-empty');
    if (tasks.length === 0) {
        if (!emptyNote) {
            emptyNote = document.createElement('div');
            emptyNote.className = 'proc-task-empty';
            listEl.after(emptyNote);
        }
        emptyNote.textContent = labels.proc_tasks_none || '';
        emptyNote.hidden = false;
    } else if (emptyNote) {
        emptyNote.hidden = true;
    }

    const existing = {};
    Array.from(listEl.children).forEach((node) => {
        if (node.dataset.taskId) {
            existing[node.dataset.taskId] = node;
        }
    });

    const seen = new Set();
    let prev = null;
    for (const task of tasks) {
        seen.add(task.id);
        let node = existing[task.id];
        if (!node) {
            node = createTaskRow(panel, task);
        }
        updateTaskRow(node, task);
        const after = prev ? prev.nextSibling : listEl.firstChild;
        if (after !== node) {
            listEl.insertBefore(node, after);
        }
        prev = node;
    }

    Array.from(listEl.children).forEach((node) => {
        if (node.dataset.taskId && !seen.has(node.dataset.taskId)) {
            panel.expanded.delete(node.dataset.taskId);
            node.remove();
        }
    });

    const dropped = total - tasks.length;
    if (dropped > 0) {
        noteEl.hidden = false;
        noteEl.textContent = fmt(labels.tasks_more, { count: dropped });
    } else {
        noteEl.hidden = true;
    }
}

function createTaskRow(panel, task) {
    const row = document.createElement('div');
    row.className = 'proc-task';
    row.dataset.taskId = task.id;
    row.innerHTML = ''
        + '<button type="button" class="proc-task-head">'
        +     '<span class="proc-task-caret" aria-hidden="true">▸</span>'
        +     '<span class="proc-task-label"></span>'
        +     '<span class="proc-task-tests"></span>'
        +     '<span class="proc-task-meta"></span>'
        + '</button>'
        + '<pre class="proc-task-output" hidden></pre>';
    row.querySelector('.proc-task-head').addEventListener('click', (event) => {
        event.stopPropagation();
        toggleTask(panel, row, task.id);
    });
    return row;
}

function updateTaskRow(row, task) {
    const labels = state.labels;
    const label = (task.label && task.label.trim()) ? task.label.trim() : fmt(labels.task_label, { id: task.id });
    row.querySelector('.proc-task-label').textContent = label;
    // The task age refreshes only every few seconds, so it is shown coarse
    // (hours/minutes) - unlike the per-second process uptime.
    const active = task.age <= TASK_ACTIVE_SECONDS;
    row.querySelector('.proc-task-meta').textContent =
        (active ? (labels.task_active || '') : fmt(labels.task_last_output, { age: formatDurationCoarse(task.age) }))
        + ' · ' + formatBytes(task.size);
    row.classList.toggle('is-active', active);

    // The newest test run's outcome, when the output ends with a runner's summary.
    const tests = logic.testsLabel(task.tests, labels);
    const testsEl = row.querySelector('.proc-task-tests');
    testsEl.textContent = tests ? tests.text : '';
    testsEl.classList.toggle('ok', Boolean(tests && tests.ok));
    testsEl.classList.toggle('failed', Boolean(tests && !tests.ok));
}

function toggleTask(panel, row, taskId) {
    const out = row.querySelector('.proc-task-output');
    if (panel.expanded.has(taskId)) {
        panel.expanded.delete(taskId);
        out.hidden = true;
        row.classList.remove('is-open');
    } else {
        panel.expanded.add(taskId);
        out.hidden = false;
        row.classList.add('is-open');
        out.textContent = state.labels.task_output_loading || '…';
        out.__rawText = null;
        callTaskOutput(panel.sessionId, panel.cwd, taskId, panel.origin).then((text) => {
            if (procBlock === panel && panel.expanded.has(taskId)) {
                setTaskOutput(panel, taskId, text);
            }
        });
    }
}

function setTaskOutput(panel, taskId, text) {
    if (!panel.expanded.has(taskId)) {
        return;
    }
    const listEl = panel.el.querySelector('.proc-task-list');
    const row = Array.from(listEl.children).find((node) => node.dataset.taskId === taskId);
    if (!row) {
        return;
    }
    const out = row.querySelector('.proc-task-output');
    if (!out || out.hidden) {
        return;
    }
    // Don't clobber a selection the user is making inside this console: skip the
    // live refresh while text here is selected, so it stays put and Ctrl+C can
    // copy it. The next tick (once nothing is selected) catches up.
    const selection = window.getSelection ? window.getSelection() : null;
    if (selection && !selection.isCollapsed && selection.rangeCount > 0
        && out.contains(selection.anchorNode) && out.contains(selection.focusNode)) {
        return;
    }
    const content = (text == null || text === '') ? (state.labels.task_output_empty || '') : text;
    // Compare against the raw text, since the rendered HTML (colored spans) is
    // no longer a faithful string to diff against.
    if (out.__rawText === content) {
        return;
    }
    // Follow the tail only when the user is already at the bottom, so scrolling
    // back to read earlier output is not yanked away on the next refresh.
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 4;
    out.__rawText = content;
    out.innerHTML = ansiToHtml(content);
    if (atBottom) {
        out.scrollTop = out.scrollHeight;
    }
}

/* Bridge calls for the panel. Each degrades to the browser-preview mocks (or an
   empty result) when there is no pywebview bridge, so ?mock still demos it. */
async function callProcessStats(pid, origin) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_process_stats === 'function') {
        try {
            return (await bridge.get_process_stats(pid, origin || 'local')) || [];
        } catch (e) {
            return [];
        }
    }
    return resolveMock(window.__MOCK_PROC_STATS__, pid) || [];
}

async function callTasks(sessionId, cwd, maxAge, origin) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.get_tasks === 'function') {
        try {
            return (await bridge.get_tasks(sessionId, cwd, maxAge || 0, origin || 'local')) || { tasks: [], total: 0 };
        } catch (e) {
            return { tasks: [], total: 0 };
        }
    }
    const tasks = resolveMock(window.__MOCK_TASKS__, sessionId) || [];
    return { tasks: tasks, total: tasks.length };
}

async function callTaskOutput(sessionId, cwd, taskId, origin) {
    const bridge = apiBridge();
    if (bridge && typeof bridge.read_task_output === 'function') {
        try {
            return await bridge.read_task_output(sessionId, cwd, taskId, origin || 'local');
        } catch (e) {
            return null;
        }
    }
    return resolveMock(window.__MOCK_TASK_OUTPUT__, taskId);
}

// The longest a real (non-VM) descendant process has been running, or 0 when
// none reports an uptime. Used as the relevance cutoff for the task list.
function maxProcessUptime(stats) {
    let max = 0;
    for (const stat of stats) {
        if (stat.kind !== 'wsl_vm' && typeof stat.uptime === 'number' && stat.uptime > max) {
            max = stat.uptime;
        }
    }
    return max;
}

// A mock entry may be a value or a thunk (so a preview can jitter live).
function resolveMock(store, key) {
    if (!store) {
        return null;
    }
    const entry = store[key];
    return typeof entry === 'function' ? entry() : entry;
}

function formatCpu(cpu) {
    if (cpu == null) {
        return '…';
    }
    return (cpu < 10 ? cpu.toFixed(1) : String(Math.round(cpu))) + '%';
}

function formatBytes(bytes) {
    if (bytes == null) {
        return '…';
    }
    if (bytes < 1024) {
        return bytes + ' B';
    }
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return (value < 10 ? value.toFixed(1) : String(Math.round(value))) + ' ' + units[unit];
}

// Always ends in seconds, since the panel ticks every second; larger units are
// zero-padded once a bigger one precedes them (e.g. "3h 07m 05s").
function formatDurationShort(seconds) {
    if (seconds == null) {
        return '…';
    }
    let total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const secs = total - minutes * 60;
    const pad = (value) => String(value).padStart(2, '0');
    if (days > 0) {
        return days + 'd ' + pad(hours) + 'h ' + pad(minutes) + 'm ' + pad(secs) + 's';
    }
    if (hours > 0) {
        return hours + 'h ' + pad(minutes) + 'm ' + pad(secs) + 's';
    }
    if (minutes > 0) {
        return minutes + 'm ' + pad(secs) + 's';
    }
    return secs + 's';
}

// Coarse form (no seconds) for values refreshed only every few seconds, so the
// text does not imply a precision it does not have.
function formatDurationCoarse(seconds) {
    if (seconds == null) {
        return '…';
    }
    let total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    total -= days * 86400;
    const hours = Math.floor(total / 3600);
    total -= hours * 3600;
    const minutes = Math.floor(total / 60);
    const pad = (value) => String(value).padStart(2, '0');
    if (days > 0) {
        return days + 'd ' + pad(hours) + 'h';
    }
    if (hours > 0) {
        return hours + 'h ' + pad(minutes) + 'm';
    }
    if (minutes > 0) {
        return minutes + 'm';
    }
    return '<1m';
}
