'use strict';

/* --- process panel ---

   Clicking the ⚙ background-process badge opens a panel anchored under it, with a
   close button (not a hover tooltip, so it survives while you read and expand
   rows). Two sections: a live per-process table (CPU / memory / uptime, polled
   once a second) and the session's background tasks. A task row expands to a
   mono-space console that tails that task's live output file once a second while
   it is open. This is the one place the UI shows process output text; the output
   is fetched on demand (read_task_output bridge), never on the snapshot poll, and
   only while a row is expanded. */
const PROC_POLL_MS = 1000;
const TASK_LIST_REFRESH_TICKS = 3;   // re-list tasks every 3rd stats tick, not every tick
const TASK_ACTIVE_SECONDS = 5;       // "active" when its output changed this recently
const TASK_AGE_GRACE_SECONDS = 60;   // keep tasks up to this much older than the oldest process

let procPanel = null;

function closeProcPanel() {
    const panel = procPanel;
    if (!panel) {
        return;
    }
    procPanel = null;
    clearTimeout(panel.timer);
    document.removeEventListener('click', panel.onDocClick, true);
    document.removeEventListener('keydown', panel.onKey, true);
    document.removeEventListener('scroll', panel.onViewportChange, true);
    window.removeEventListener('resize', panel.onViewportChange);
    if (panel.anchorEl) {
        panel.anchorEl.classList.remove('proc-badge-open');
    }
    panel.el.remove();
}

// Clicking the badge that already owns the open panel toggles it shut.
function toggleProcPanel(anchor) {
    const sameAnchor = procPanel && procPanel.anchorEl === anchor;
    closeProcPanel();
    if (sameAnchor) {
        return;
    }
    closeMenu();
    openProcPanel(anchor);
}

function openProcPanel(anchor) {
    const row = anchor.closest('.row');
    const el = document.createElement('div');
    el.className = 'proc-panel';
    el.setAttribute('role', 'dialog');
    el.innerHTML = ''
        + '<div class="proc-panel-head">'
        +     '<span class="proc-panel-title">'
        +         '<span class="proc-panel-count"></span>'
        +         '<span class="proc-panel-session"></span>'
        +     '</span>'
        +     '<button type="button" class="proc-panel-close" aria-label=""></button>'
        + '</div>'
        + '<div class="proc-panel-body">'
        +     '<section class="proc-section proc-section-processes">'
        +         '<div class="proc-section-title"></div>'
        +         '<table class="proc-table">'
        +             '<thead><tr>'
        +                 '<th class="proc-name"></th><th class="proc-num"></th>'
        +                 '<th class="proc-num"></th><th class="proc-num"></th>'
        +             '</tr></thead>'
        +             '<tbody></tbody>'
        +         '</table>'
        +     '</section>'
        +     '<section class="proc-section proc-section-tasks">'
        +         '<div class="proc-section-title"></div>'
        +         '<div class="proc-task-list"></div>'
        +         '<div class="proc-task-note" hidden></div>'
        +     '</section>'
        + '</div>';

    const labels = state.labels;
    el.querySelector('.proc-panel-close').setAttribute('aria-label', labels.proc_close || 'Close');
    el.querySelector('.proc-panel-close').textContent = '×';
    el.querySelector('.proc-section-processes .proc-section-title').textContent = labels.proc_processes_title || '';
    el.querySelector('.proc-section-tasks .proc-section-title').textContent = labels.proc_tasks_title || '';
    const headCells = el.querySelectorAll('.proc-table thead th');
    headCells[0].textContent = labels.proc_col_process || '';
    headCells[1].textContent = labels.proc_col_cpu || '';
    headCells[2].textContent = labels.proc_col_memory || '';
    headCells[3].textContent = labels.proc_col_uptime || '';

    // Which session this panel belongs to - read from the row's name so the
    // header still identifies it once the panel covers the row. Stable for the
    // panel's life (the session identity does not change), so set once.
    const nameEl = row ? row.querySelector('.name') : null;
    const sessionName = nameEl ? nameEl.textContent.trim() : '';
    if (sessionName) {
        // The gap before the dash comes from CSS margin, not a leading space -
        // a flex item trims its leading whitespace, which glued "6" to "-".
        el.querySelector('.proc-panel-session').textContent = '· ' + sessionName;
    }

    document.body.appendChild(el);
    anchor.classList.add('proc-badge-open');

    const panel = {
        el,
        anchorEl: anchor,
        rowKey: row ? row.dataset.key : null,
        pid: Number(anchor.dataset.procPid),
        sessionId: anchor.dataset.procSession || '',
        cwd: anchor.dataset.procCwd || '',
        origin: anchor.dataset.procOrigin || 'local',
        expanded: new Set(),
        tick: 0,
        timer: null,
    };
    procPanel = panel;

    el.querySelector('.proc-panel-close').addEventListener('click', (event) => {
        event.stopPropagation();
        closeProcPanel();
    });

    panel.onDocClick = (event) => {
        if (!el.contains(event.target) && !anchor.contains(event.target) && event.target !== anchor) {
            closeProcPanel();
        }
    };
    panel.onKey = (event) => {
        if (event.key === 'Escape') {
            closeProcPanel();
        }
    };
    // Follow the anchor on scroll/resize (a background scroll moves the row); a
    // scroll inside the panel's own consoles just repositions to the same spot.
    panel.onViewportChange = () => {
        if (panel.anchorEl && panel.anchorEl.isConnected) {
            positionProcPanel(el, panel.anchorEl);
        }
    };

    // Defer the outside-click listener so the opening click does not close it.
    setTimeout(() => {
        if (procPanel === panel) {
            document.addEventListener('click', panel.onDocClick, true);
        }
    }, 0);
    document.addEventListener('keydown', panel.onKey, true);
    document.addEventListener('scroll', panel.onViewportChange, true);
    window.addEventListener('resize', panel.onViewportChange);

    positionProcPanel(el, anchor);
    procPanelLoop(panel);
}

// Self-scheduling poll so a slow tick never overlaps the next. Fills the panel
// immediately, then again every PROC_POLL_MS while it stays open.
async function procPanelLoop(panel) {
    if (procPanel !== panel) {
        return;
    }
    await refreshProcPanel(panel);
    if (procPanel !== panel) {
        return;
    }
    panel.timer = setTimeout(() => procPanelLoop(panel), PROC_POLL_MS);
}

async function refreshProcPanel(panel) {
    panel.tick += 1;

    const stats = await callProcessStats(panel.pid, panel.origin);
    if (procPanel !== panel) {
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
        if (procPanel !== panel) {
            return;
        }
        renderTaskList(panel, result);
    }

    for (const taskId of Array.from(panel.expanded)) {
        const text = await callTaskOutput(panel.sessionId, panel.cwd, taskId, panel.origin);
        if (procPanel !== panel) {
            return;
        }
        setTaskOutput(panel, taskId, text);
    }

    positionProcPanel(panel.el, panel.anchorEl);
}

function renderProcTable(panel, stats) {
    const labels = state.labels;
    // The WSL2-VM row is context, not one of the session's processes, so the
    // count reflects only the real descendant processes (matching the badge).
    const processCount = stats.filter((stat) => stat.kind !== 'wsl_vm').length;
    const count = panel.el.querySelector('.proc-panel-count');
    if (count) {
        count.textContent = fmt(labels.processes_running, { count: processCount });
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
    const section = panel.el.querySelector('.proc-section-tasks');
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
            if (procPanel === panel && panel.expanded.has(taskId)) {
                setTaskOutput(panel, taskId, text);
            }
        });
    }
    positionProcPanel(panel.el, panel.anchorEl);
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

function positionProcPanel(el, anchor) {
    const rect = anchor.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const width = el.offsetWidth;
    const height = el.offsetHeight;

    let left = Math.min(rect.left, window.innerWidth - width - margin);
    left = Math.max(margin, left);

    let top = rect.bottom + gap;
    if (top + height + margin > window.innerHeight) {
        const above = rect.top - height - gap;
        top = above >= margin ? above : Math.max(margin, window.innerHeight - height - margin);
    }

    el.style.left = Math.round(left) + 'px';
    el.style.top = Math.round(Math.max(margin, top)) + 'px';
}

// Re-anchor after a render (rows reorder but nodes are reused); close if the
// badge is gone (the session ended or its process count dropped to zero).
function syncProcPanel() {
    const panel = procPanel;
    if (!panel) {
        return;
    }
    const anchor = findProcBadge(panel.rowKey);
    if (!anchor) {
        closeProcPanel();
        return;
    }
    if (anchor !== panel.anchorEl) {
        panel.anchorEl.classList.remove('proc-badge-open');
        anchor.classList.add('proc-badge-open');
        panel.anchorEl = anchor;
    }
    positionProcPanel(panel.el, anchor);
}

function findProcBadge(rowKey) {
    if (!rowKey) {
        return null;
    }
    const rows = document.querySelectorAll('.row');
    for (const row of rows) {
        if (row.dataset.key === rowKey) {
            return row.querySelector('.proc-badge');
        }
    }
    return null;
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
