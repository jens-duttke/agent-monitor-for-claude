'use strict';

/* --- rendering --- */

// Host as plain text, e.g. "VS Code › CLI" for a terminal-driven session
// inside VS Code. "CLI" is a proper name, not translated.
function hostText(session) {
    let text = session.host || '';
    if (session.via_cli) {
        text = text ? text + ' › CLI' : 'CLI';
    }
    if (session.kind && session.kind !== 'interactive') {
        const labelKey = logic.kindLabelKey(session.kind);
        const kindLabel = (labelKey && state.labels[labelKey]) || session.kind;
        text = text ? text + ' · ' + kindLabel : kindLabel;
    }
    return text;
}

// A transcript UTC timestamp as a local date-time with seconds, e.g. for the
// model-switch history. Falls back to the raw value if it will not parse.
function fmtDateTime(iso) {
    const date = new Date(iso);
    if (isNaN(date.getTime())) {
        return String(iso == null ? '' : iso);
    }
    return date.toLocaleString(undefined, {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
}

// The model column: the current model (left), plus a "+N" badge (right, like
// the mode chip) when the session switched models. They share one cell that
// spreads them apart with space-between, so the name stays left-aligned and the
// badge right-aligned across rows. Hovering the badge lists the switch timeline
// oldest first - one line per model run with the time it began, so a model
// returned to appears again and the last line is the current model.
function modelCellHtml(session) {
    const name = '<span class="model-name">' + esc(session.model || '') + modelSuffixHtml(session) + '</span>';
    if (!session.model_switched) {
        return name;
    }
    const history = session.model_history || [];
    const lines = history.map((entry) => fmtDateTime(entry.time) + '  ' + entry.label);
    return name + '<span class="model-more"' + attr('data-tip', lines.join('\n')) + '>+' + esc(history.length - 1) + '</span>';
}

// The latest reply's effort level and fast mode, after the model name in a
// dimmer weight - the two settings that decide how that model is run. The
// header already names the default effort, so a row names its own only when it
// differs.
function modelSuffixHtml(session) {
    const labels = state.labels;
    const parts = [];
    const lines = [];
    if (session.effort && session.effort !== state.defaultEffort) {
        parts.push(session.effort);
        lines.push(fmt(labels.effort_tip, { level: session.effort }));
    }
    if (session.fast_mode) {
        parts.push('fast');
        lines.push(labels.fast_mode_tip);
    }
    if (!session.model || parts.length === 0) {
        return '';
    }
    return ' <span class="model-effort"' + attr('data-tip', lines.join('\n')) + '>' + esc(parts.join(' · ')) + '</span>';
}

// The Claude Code version column, shown only while more than one version is in
// view (see cliColumnRelevant). The version text links to its own changelog
// section, and a "+N" badge - the model column's, one column over - lists the
// versions a session spanned when it outlived a CLI update, each with where it
// was written from, which is what tells an update apart from two processes
// writing the same session at once.
function cliCellHtml(session) {
    const version = session.cli_version || '';
    if (!version) {
        return '';
    }

    // The application requests nothing from this URL; the click hands it to the
    // host, which opens it in the system browser. target="_blank" is what routes
    // it there - a same-window navigation would replace the app's own page
    // instead. A version that is not a plain release number yields no URL and is
    // rendered as text, so the href can only ever hold digits and dots.
    const url = logic.changelogUrl(version);
    let html;
    if (url) {
        html = '<a class="cli-link"' + attr('href', url) + ' target="_blank" rel="noreferrer"'
            + attr('data-tip', fmt(state.labels.cli_changelog_tip, { version: version })) + '>' + esc(version) + '</a>';
    } else {
        html = '<span class="cli-name">' + esc(version) + '</span>';
    }

    if (!session.cli_switched) {
        return html;
    }

    const history = session.cli_history || [];
    const lines = history.map((entry) => fmtDateTime(entry.time) + '  ' + entry.version + (entry.origin ? ' · ' + entry.origin : ''));
    return html + '<span class="cli-more"' + attr('data-tip', lines.join('\n')) + '>+' + esc(history.length - 1) + '</span>';
}

// A mouse pointer, the mark Claude Desktop also uses for a session reachable
// through Remote Control.
const REMOTE_CONTROL_SVG = '<svg class="rc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M4 4l16 6.5-6.5 2-2 7.5z"></path></svg>';

// A git branch: two commits on a line and a third forking off it.
const BRANCH_SVG = '<svg class="branch-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<circle cx="6" cy="5" r="2"></circle><circle cx="6" cy="19" r="2"></circle><circle cx="18" cy="7" r="2"></circle>'
    + '<path d="M6 7v10"></path><path d="M18 9c0 5-12 3-12 8"></path></svg>';

function branchHtml(branch) {
    return BRANCH_SVG + '<span class="branch-name">' + esc(branch) + '</span>';
}

// The branch's tooltip: the branch and, when it names a ticket, what that
// ticket's sessions cost.
function branchTip(branch) {
    const ticket = logic.ticketTip(branch, state.ticketTotals, state.ticketIncludesHistory, state.labels);
    return fmt(state.labels.branch_tip, { branch: branch }) + (ticket ? '\n' + ticket : '');
}

function nameCellHtml(session) {
    const labels = state.labels;
    // The title's tooltip repeats the full title, which the row may cut short,
    // and adds the details that have no column of their own.
    let html = '<span class="name"' + attr('data-tip', session.info_tip || '') + '>' + esc(session.name) + '</span>';

    if (session.remote_control) {
        html += '<span class="rc-badge"' + attr('data-tip', labels.remote_control_tip) + '>' + REMOTE_CONTROL_SVG + '</span>';
    }

    // The panel is the project the session was launched in; the badge names the
    // git worktree it switched into, with the full path (and the branch, when
    // Claude Code recorded one) on hover.
    if (session.worktree) {
        const lines = [labels.worktree_tip, session.worktree.path];
        if (session.worktree.branch) {
            lines.push(fmt(labels.worktree_branch, { branch: session.worktree.branch }));
        }
        html += '<span class="worktree-badge"' + attr('data-tip', lines.join('\n')) + '>⎇ ' + esc(session.worktree.name) + '</span>';
    }

    // Only a panel whose sessions work on different branches names them per row;
    // a shared one sits in the panel header instead.
    if (session.show_branch) {
        html += '<span class="branch-badge"' + attr('data-tip', branchTip(session.branch)) + '>'
            + branchHtml(session.branch) + '</span>';
    }

    html += pullRequestHtml(session.pull_requests, labels);

    if (session.mode) {
        html += '<span class="mode-chip">' + esc(session.mode) + '</span>';
    }

    // Auto mode puts itself on hold after a run of classifier blocks and starts
    // prompting for everything again. Called out next to the mode chip it
    // contradicts: without it the session reads "Auto" while asking about every
    // step, which is exactly the state that looks like a misconfigured setting.
    if (session.auto_paused) {
        const lines = [labels.auto_paused];
        if (session.auto_denials_total > 0) {
            lines.push(fmt(labels.auto_denials, { count: session.auto_denials_total }));
        }
        html += '<span class="auto-paused-badge"' + attr('data-tip', lines.join('\n')) + '>⏸</span>';
    }

    // Running-subagent badge with a tooltip listing what each one is doing. For a
    // background workflow the badge shows the run's total agent count - stable
    // across the fan-out phases where no single agent is momentarily running, so
    // it no longer flickers away - and the tooltip leads with that total.
    if (session.subagents_running > 0 || session.workflow_active) {
        const lines = [];
        if (session.workflow_active) {
            lines.push(fmt(labels.subagents_workflow, { done: session.workflow_done, total: session.workflow_total }));
        }
        if (session.subagents_running > 0) {
            lines.push(fmt(labels.subagents_running, { count: session.subagents_running }));
        }
        (session.subagents_labels || []).forEach((label) => lines.push('• ' + label));
        // "Recently finished" is the agent-file scan's recent-window count; for a
        // workflow the "{done}/{total}" line above already conveys progress, so
        // this line is only for the plain-subagent case (never the cumulative
        // workflow total mislabelled as "recently").
        if (!session.workflow_active && session.subagents_done > 0) {
            lines.push(fmt(labels.subagents_finished, { count: session.subagents_done }));
        }
        const badgeCount = session.workflow_active ? session.workflow_total : session.subagents_running;
        html += '<span class="agents-badge"' + attr('data-tip', lines.join('\n')) + '>⚡ ' + esc(badgeCount) + '</span>';
    }

    // Background OS processes the session is running (e.g. a watched build).
    // Clicking the badge opens the process panel (CPU/RAM/uptime plus the
    // session's background-task output); the pid/session/cwd it needs ride along
    // as data attributes. data-tip gives a hover hint about the click action.
    if (session.processes > 0) {
        html += '<span class="proc-badge"'
            + attr('data-proc-pid', session.pid)
            + attr('data-proc-session', session.session_id)
            + attr('data-proc-cwd', session.cwd)
            + attr('data-proc-origin', session.origin || 'local')
            + attr('data-tip', labels.proc_open_hint || '')
            + '>⚙ ' + esc(session.processes) + '</span>';
    }

    return html + detailBadgesHtml(session, labels);
}

// The newest pull request Claude Code linked, as a link to it when its address
// passed logic.pullRequestUrl; the tooltip lists every one. Like the changelog
// link, the application requests nothing from the address: target="_blank" hands
// it to the host, which opens the system browser.
function pullRequestHtml(pullRequests, labels) {
    if (!pullRequests || pullRequests.length === 0) {
        return '';
    }
    const newest = pullRequests[0];
    const lines = [labels.pr_tip].concat(pullRequests.map((pr) => pr.repository + ' #' + pr.number));
    const text = '#' + newest.number + (pullRequests.length > 1 ? ' +' + (pullRequests.length - 1) : '');
    if (!newest.url) {
        return '<span class="pr-badge"' + attr('data-tip', lines.join('\n')) + '>' + esc(text) + '</span>';
    }
    return '<a class="pr-badge pr-link"' + attr('href', newest.url) + ' target="_blank" rel="noreferrer"'
        + attr('data-tip', lines.join('\n')) + '>' + esc(text) + '</a>';
}

// Badges for the details worth seeing at a glance: a nearly full context, a
// lapsed prompt cache the next prompt pays for, the plan's step, queued prompts,
// the budget, hooks that keep failing, and a newer Claude Code a restart would
// bring.
function detailBadgesHtml(session, labels) {
    let html = '';

    if (session.context_warn) {
        html += '<span class="context-badge"' + attr('data-tip', logic.contextLine(session.context, labels)) + '>◔ '
            + esc(Math.round(session.context.ratio * 100)) + '%</span>';
    }

    if (session.cache_warn) {
        html += '<span class="cache-badge"' + attr('data-tip', logic.cacheLine(session.cache, labels)) + '>❄ '
            + esc(logic.formatCost(session.cache.rewrite_usd)) + '</span>';
    }

    if (session.plan) {
        html += '<span class="plan-badge"' + attr('data-tip', fmt(labels.plan_tip, session.plan)) + '>☰ '
            + esc(session.plan.step) + '/' + esc(session.plan.total) + '</span>';
    }

    if (session.queued > 0) {
        html += '<span class="queue-badge"' + attr('data-tip', fmt(labels.queued_tip, { count: session.queued })) + '>✉ '
            + esc(session.queued) + '</span>';
    }

    if (session.budget) {
        const used = logic.formatCost(session.budget.used);
        const total = logic.formatCost(session.budget.total);
        const warn = session.budget.ratio >= logic.NEAR_LIMIT_RATIO ? ' budget-warn' : '';
        html += '<span' + attr('class', 'budget-badge' + warn) + attr('data-tip', fmt(labels.budget_tip, { used: used, total: total })) + '>'
            + esc(used) + ' / ' + esc(total) + '</span>';
    }

    if (session.hook_errors.length > 0) {
        const lines = [labels.hook_errors_tip].concat(session.hook_errors.map((error) => fmt(labels.hook_error_line, {
            hook: error.hook,
            code: error.exit_code === null ? '?' : error.exit_code,
            count: error.count,
        })));
        const total = session.hook_errors.reduce((sum, error) => sum + error.count, 0);
        html += '<span class="hook-badge"' + attr('data-tip', lines.join('\n')) + '>⚠ ' + esc(total) + '</span>';
    }

    // The session still runs an older Claude Code than the one a restart would
    // bring - installed, or already running in a sibling session of the same app.
    if (session.cli_update) {
        html += '<span class="update-badge"' + attr('data-tip', fmt(labels.cli_update_tip, session.cli_update)) + '>↻ '
            + esc(session.cli_update.available) + '</span>';
    }

    return html;
}

function createRow() {
    const row = document.createElement('div');
    row.className = 'row';
    // No status-text column: the dot carries the status (label on hover),
    // and the filter chips in the top bar teach the colors.
    row.innerHTML = '<span class="dot"></span>'
        + '<div class="main-cell">'
        +     '<span class="name-cell"><span class="name"></span></span>'
        +     '<span class="usage-cell">'
        +         '<span class="usage-detail-wrap"><span class="usage-detail"></span></span>'
        +         '<span class="usage-compact"></span>'
        +     '</span>'
        + '</div>'
        + '<span class="model-cell"></span>'
        + '<span class="cli-cell"></span>'
        + '<span class="host-cell"></span>'
        + '<span class="age"></span>'
        + '<button class="row-menu-btn" type="button">&#8943;</button>'
        // Where a content search lists what it found in this session. Always in
        // the DOM so reconciliation reuses it; empty (and out of the grid flow)
        // for every row with no hits to show, which is every row until a search
        // runs.
        + '<div class="row-hits"></div>';

    bindUsageHover(row.querySelector('.usage-cell'));
    return row;
}

// Inline-expand the usage figure into its full breakdown on a deliberate hover
// (>500ms), then collapse on leave. Bound once per row node in createRow -
// reconciliation reuses the node, so this never re-binds or stacks listeners.
function bindUsageHover(cell) {
    let timer = null;
    cell.addEventListener('mouseenter', () => {
        clearTimeout(timer);
        timer = setTimeout(() => cell.classList.add('open'), 500);
    });
    cell.addEventListener('mouseleave', () => {
        clearTimeout(timer);
        cell.classList.remove('open');
    });
}

function updateRow(row, session, projectName) {
    const labels = state.labels;

    row.className = 'row status-' + session.status + (session.is_history ? ' is-history' : '');
    row.dataset.project = projectName || '';
    row.dataset.session = session.session_id || '';
    row.dataset.title = session.title || '';
    // buildSession always resolves an origin ('local' by default), so every
    // row - live or history - carries it: focusSession and the row menu's
    // transcript/scratchpad/delete actions all need it to route their bridge call.
    row.dataset.origin = session.origin || 'local';
    // A history session has no live process, so it is not a focus target (no
    // data-pid); the click-to-focus handler keys on .row[data-pid].
    // A background session is the second row without a focus target: it runs
    // under the daemon with no window of its own, and the title fallback in
    // focus_session_window could otherwise raise an unrelated terminal that
    // happens to match. Its handle is the attach command in the row menu.
    if (session.is_history || session.background) {
        delete row.dataset.pid;
    } else {
        row.dataset.pid = Number(session.pid);
    }
    if (session.vscode_deeplink) {
        row.dataset.deeplink = '1';
    } else {
        delete row.dataset.deeplink;
    }

    // The dot is now the primary status signal, so it names the status itself -
    // on an API error with the error's own line beneath the label (status_tip),
    // which is the one place there is room for it.
    const dot = row.querySelector('.dot');
    if (session.status_tip) {
        dot.dataset.tip = session.status_tip;
    } else {
        delete dot.dataset.tip;
    }

    row.querySelector('.name-cell').innerHTML = nameCellHtml(session);
    const usageCompact = row.querySelector('.usage-compact');
    usageCompact.textContent = session.usage_compact || '';
    // What the cost is made of - main conversation, subagents, skills, web
    // searches - when there is more to it than the total.
    if (session.cost_tip) {
        usageCompact.dataset.tip = session.cost_tip;
    } else {
        delete usageCompact.dataset.tip;
    }
    row.querySelector('.usage-detail').textContent = session.usage_detail || '';
    row.querySelector('.model-cell').innerHTML = modelCellHtml(session);
    row.querySelector('.cli-cell').innerHTML = cliCellHtml(session);
    const hostCell = row.querySelector('.host-cell');
    hostCell.textContent = hostText(session);
    // A WSL session's host names its distribution; the tooltip explains what
    // that means (files read over the UNC path, nothing executed there) so the
    // unfamiliar host name is never a dead end.
    if (session.wsl) {
        hostCell.dataset.tip = fmt(labels.host_wsl_tip, { distro: session.origin_label || 'WSL' });
    } else {
        delete hostCell.dataset.tip;
    }

    // A live row's age was captured at the latest snapshot (receivedAt); a
    // history row's at the one-time history fetch (historyReceivedAt). Storing
    // that capture epoch per row lets tickAges advance each age from the right
    // moment, so history ages keep growing instead of freezing at fetch time.
    const ageEl = row.querySelector('.age');
    if (session.age_seconds != null) {
        const ageAt = session.is_history ? state.historyReceivedAt : state.receivedAt;
        ageEl.dataset.age = session.age_seconds;
        ageEl.dataset.ageAt = ageAt != null ? ageAt : Date.now();
        ageEl.textContent = logic.formatAgeSince(session.age_seconds, Number(ageEl.dataset.ageAt), Date.now(), labels);
    } else {
        delete ageEl.dataset.age;
        delete ageEl.dataset.ageAt;
        ageEl.textContent = '';
    }

    const menuBtn = row.querySelector('.row-menu-btn');
    menuBtn.dataset.session = session.session_id || '';
    menuBtn.dataset.cwd = session.cwd || '';
    menuBtn.dataset.origin = session.origin || 'local';
    if (session.job_id) {
        menuBtn.dataset.job = session.job_id;
    } else {
        delete menuBtn.dataset.job;
    }
    // Only a history row (a past, non-live session with no registry record) may
    // be deleted; the menu adds its delete item off this flag.
    if (session.is_history) {
        menuBtn.dataset.history = '1';
    } else {
        delete menuBtn.dataset.history;
    }
    menuBtn.setAttribute('aria-label', labels.row_menu || 'More actions');

    // The passages a content search found in this session, if one is active and
    // has settled on this row. Cleared for every other row, so an old result
    // never lingers under a session the current query did not match.
    //
    // Rewritten only when the result itself is a different object, the way the
    // task console diffs its raw text and for the same two reasons: the excerpts
    // are meant to be selected and copied, and rewriting them on the per-second
    // poll would drop the selection mid-drag - and a match is never mutated in
    // place (onSearchPush keeps the first result per session), so identity is an
    // exact test rather than an approximation of one.
    const hitsEl = row.querySelector('.row-hits');
    const hit = searchHitFor(session.session_id);
    if (hitsEl.__searchHit !== hit) {
        hitsEl.__searchHit = hit;
        hitsEl.innerHTML = logic.searchHitsMarkup(hit, labels);
    }
}

// This session's search result, or null when no search matched it. A row only
// appears once its own match has arrived, so a result is complete whenever there
// is a row to put it under - a running scan adds rows, it never fills in old ones.
function searchHitFor(sessionId) {
    if (!(state.searchMatches instanceof Map)) {
        return null;
    }
    return state.searchMatches.get(sessionId) || null;
}

function topStatus(sessions) {
    let top = null;
    for (const session of sessions) {
        if (top === null || (logic.STATUS_ORDER[session.status] ?? 99) < (logic.STATUS_ORDER[top.status] ?? 99)) {
            top = session;
        }
    }
    return top;
}

function createPanel() {
    const section = document.createElement('section');
    section.className = 'panel';
    section.innerHTML = '<div class="panel-head">'
        + '<h2></h2><span class="panel-path"><span class="path-open"></span></span>'
        + '<span class="panel-branch"></span>'
        + '<span class="panel-origin"></span><span class="head-status"></span>'
        + '<span class="panel-count"></span><span class="chevron"></span>'
        + '</div><div class="rows"></div>';
    return section;
}

function updatePanel(section, project) {
    const collapsed = state.collapsed.has(project.key);
    const anyNeeds = project.sessions.some((session) => session.needs_attention);

    section.classList.toggle('needs', anyNeeds);
    section.classList.toggle('collapsed', collapsed);

    const head = section.querySelector('.panel-head');
    // Groups are keyed per origin (logic.groupKey), so every session in this
    // panel shares one origin and the header's open-folder click can never
    // land in another root - two distros' identical POSIX paths form two
    // panels. The key is the panel's stable identity (collapse state,
    // reconciliation); the cwd stays the real path the click opens.
    head.dataset.key = project.key;
    head.dataset.cwd = project.cwd;
    head.dataset.origin = project.origin || 'local';
    section.querySelector('h2').textContent = project.name;
    section.querySelector('.panel-count').textContent = project.sessions.length;

    // The path text is its own click target (opens the folder in the file manager);
    // clicking elsewhere in the header still toggles the panel.
    const pathOpen = section.querySelector('.path-open');
    pathOpen.textContent = project.cwd;
    pathOpen.dataset.tip = state.labels.show_in_file_manager || 'Show in file manager';

    // Names the distro when the panel belongs to a WSL root, so two panels
    // with the same path text stay tellable apart at a glance.
    section.querySelector('.panel-origin').textContent = project.origin_display || '';

    // The branch every session of the panel is on, when they share one; rewritten
    // only on a change, so a hovered tooltip survives the poll.
    const branchEl = section.querySelector('.panel-branch');
    if (branchEl.dataset.branch !== (project.branch || '')) {
        branchEl.dataset.branch = project.branch || '';
        branchEl.innerHTML = project.branch ? branchHtml(project.branch) : '';
    }
    if (project.branch) {
        branchEl.dataset.tip = branchTip(project.branch);
    } else {
        delete branchEl.dataset.tip;
    }

    // The group's headline status, shown in both states: collapsed it stands in
    // for the hidden rows, open it names at a glance what the panel as a whole
    // is doing - which is what the header is read for either way.
    const headStatus = section.querySelector('.head-status');
    const top = topStatus(project.sessions);
    headStatus.className = 'head-status' + (top ? ' status-' + top.status : '');
    headStatus.textContent = top ? top.status_label : '';

    const rows = section.querySelector('.rows');
    if (collapsed) {
        rows.replaceChildren();
    } else {
        reconcile(rows, project.sessions, rowKey, createRow, (el, session) => updateRow(el, session, project.name));
    }
}

// One registry file per process, so the pid is a row's stable, unique identity.
// The session id alone is not unique: the same session open in two places - its
// VS Code window and a `claude --resume` in a terminal - yields two live records
// that share a session id but are distinct processes. Keying on the id alone
// would collide, and duplicate keys break the reconcile pass below (an orphaned
// node is never removed, so the list would grow by one per render without
// bound). Pairing the id with the pid keeps every record's key distinct.
function rowKey(session) {
    return (session.session_id || 'nosid') + '#' + session.pid;
}

// Keyed reconciliation: reuse existing child nodes (matched by data-key),
// create only new ones, remove departed ones, then order to match `items`.
// Reusing nodes is what lets an open row menu and the scroll position survive
// a refresh, and avoids the flicker of a full innerHTML rebuild.
function reconcile(container, items, keyOf, create, update) {
    const existing = new Map();
    for (const child of Array.from(container.children)) {
        if (child.dataset && child.dataset.key != null) {
            existing.set(child.dataset.key, child);
        }
    }

    const ordered = [];
    const seen = new Map();
    for (const item of items) {
        // Duplicate keys would otherwise leak nodes forever: two children end
        // up sharing one data-key, the lookup map keeps only one of them, and
        // the other is never matched NOR removed - one orphan per render,
        // without bound. Suffixing the duplicate degrades that to a harmless
        // re-created node instead.
        let key = String(keyOf(item));
        const dupes = seen.get(key) || 0;
        seen.set(key, dupes + 1);
        if (dupes > 0) {
            key += '#dup' + dupes;
        }

        let el = existing.get(key);
        if (el) {
            existing.delete(key);
        } else {
            el = create(item);
            el.dataset.key = key;
        }
        update(el, item);
        ordered.push(el);
    }

    existing.forEach((el) => el.remove());

    ordered.forEach((el, index) => {
        if (container.children[index] !== el) {
            container.insertBefore(el, container.children[index] || null);
        }
    });
}

function setHero(blocked) {
    const links = blocked.map(({ session, projectName }) =>
        '<button class="hero-link"'
        + attr('data-pid', Number(session.pid))
        + attr('data-project', projectName)
        + attr('data-session', session.session_id || '')
        + attr('data-title', session.title || '')
        + attr('data-origin', session.origin || 'local')
        + (session.vscode_deeplink ? attr('data-deeplink', '1') : '')
        + attr('data-tip', session.status_tip)
        + '>'
        + esc(session.name) + '</button>'
    ).join('');

    heroSlot.innerHTML = '<div class="hero"><span class="hero-icon">&#128276;</span>'
        + '<span class="hero-text">' + esc(fmt(state.labels.feedback_needed, { count: blocked.length })) + '</span>'
        + '<span class="hero-links">' + links + '</span></div>';
}

function persistCollapsed() {
    try {
        localStorage.setItem('amc-collapsed', JSON.stringify([...state.collapsed]));
    } catch (e) { /* storage unavailable */ }
}

function toggleCollapse(key) {
    if (!key) {
        return;
    }
    if (state.collapsed.has(key)) {
        state.collapsed.delete(key);
    } else {
        state.collapsed.add(key);
    }
    persistCollapsed();
    if (state.last) {
        render(state.last);
    }
}

// The one place UI state is mapped onto the plain data the pure markup builders
// in logic.js take. Keeping the mapping here (and the wording rules there) is
// what lets the empty-state and notice wording be tested without a DOM.
function emptyStateView(searchActive, quietMessage, loadingNote) {
    return {
        // The same four keys logic.searchWideningStep reads, so the widening
        // decision is made once, inside the markup, from this one object.
        allKeys: FILTER_KEYS,
        activeKeys: state.filters,
        includeHidden: state.searchIncludeHidden,
        historyRange: state.historyRange,
        searchActive,
        // A pending debounce counts as running: until that scan starts, the rows
        // are still filtered by the PREVIOUS query's matches, so an empty view
        // says nothing about the one being typed - and "not found", complete
        // with an offer to widen, would flicker in and out between keystrokes.
        searching: Boolean(state.searchLoading || state.searchTimer),
        quietMessage,
        loadingNote,
        labels: state.labels,
    };
}

// Column variable -> cell selector. After each render the widest cell per
// column (across ALL panels) is measured and written to the CSS variable, so
// every row shares identical column widths without wasting space.
const COLUMN_CELLS = [
    ['--col-model', '.row .model-cell'],
    ['--col-cli', '.row .cli-cell'],
    ['--col-host', '.row .host-cell'],
];

function alignColumns() {
    const rootStyle = document.documentElement.style;

    // Let every cell take its natural width, measure, then lock the maximum.
    for (const [variable] of COLUMN_CELLS) {
        rootStyle.setProperty(variable, 'max-content');
    }

    const widths = COLUMN_CELLS.map(([, selector]) => {
        let max = 0;
        document.querySelectorAll(selector).forEach((cell) => {
            max = Math.max(max, cell.offsetWidth);
        });
        return max;
    });

    COLUMN_CELLS.forEach(([variable], index) => {
        rootStyle.setProperty(variable, widths[index] ? Math.ceil(widths[index]) + 'px' : 'max-content');
    });
}

// Stable shell created once; reconciliation happens inside the panels slot.
let heroSlot = null;
let noticeSlot = null;
let panelsSlot = null;
let stateSlot = null;

function ensureShell() {
    if (panelsSlot && panelsSlot.isConnected) {
        return;
    }
    const content = document.getElementById('content');
    content.innerHTML = '<div class="hero-slot"></div><div class="notice-slot"></div>'
        + '<div class="panels-slot"></div><div class="state-slot"></div>';
    heroSlot = content.querySelector('.hero-slot');
    noticeSlot = content.querySelector('.notice-slot');
    panelsSlot = content.querySelector('.panels-slot');
    stateSlot = content.querySelector('.state-slot');
}

function render(snapshot) {
    const previous = state.last;
    state.last = snapshot;

    const prices = logic.resolvePrices(state.pricing, todayIso());

    // The content search narrows the whole view at once - the chip counts, the
    // blocked banner, and the rows all reflect only sessions the backend matched
    // (matches stream in, so the set grows live). A query below the minimum, or
    // no query, matches everything. It combines with the status chips: a session
    // shows only when both its chip is on and its content matched.
    const searchActive = state.search.trim().length >= SEARCH_MIN_CHARS;
    const matchesSearch = (session) => logic.sessionMatchesSearch(session.session_id, searchActive, state.searchMatches);

    // A widened search read past the active chips, so its extra hits must also
    // be allowed past the row filter - finding a session and then still hiding
    // it would make the whole offer pointless. Only a settled hit counts: while
    // a scan runs, matchesSearch passes everything, which would briefly show
    // every hidden session.
    const widened = searchActive && state.searchIncludeHidden;
    const isWidenedHit = (session) => widened
        && state.searchMatches instanceof Map
        && state.searchMatches.has(session.session_id);

    // The past sessions the current window covers, minus any that came back to
    // life. Resolved whether or not the chip is on: with it off they stay out of
    // the view but still feed the chip's count (below).
    const historyActive = state.filters.has('history');
    const historyRows = Array.isArray(state.history)
        ? logic.pruneResumedHistory(visibleHistory(), snapshot.sessions || [])
        : [];

    // Fold past sessions in while the history chip is active - or while a
    // widened search reaches into them, in which case only the ones it matched
    // pass the row filter below. groupProjects then places them under their own
    // project panels, marked (and, in updateRow, styled) as history rows.
    const foldHistory = (historyActive || widened) && Array.isArray(state.history);
    let rawSessions = snapshot.sessions || [];
    if (foldHistory) {
        rawSessions = rawSessions.concat(historyRows);
        // A previously-live session that just left the snapshot (ended and its
        // registry record pruned) was excluded from the one-shot history fetch;
        // re-fetch so it can move into history instead of vanishing from both.
        if (previous && logic.historyNeedsRefresh(previous.sessions, snapshot.sessions)) {
            state.history = null;
            state.historyLoadedSeconds = undefined;
            ensureHistoryLoaded();
        }
    }
    const loadingNote = ((historyActive || widened) && state.historyLoading) ? state.labels.history_loading : '';

    // A ticket's cost covers every session on it whatever the filters show: the
    // live ones, and the past ones in the window once they have been loaded.
    state.ticketTotals = logic.ticketTotals((snapshot.sessions || []).concat(historyRows), prices);
    state.ticketIncludesHistory = Array.isArray(state.history);

    const projects = logic.groupProjects(rawSessions, state.labels, prices);

    const counts = countByFilter(projects, matchesSearch);
    // Every status filter counts its sessions whether or not it is on, because
    // its sessions are in the grouped set either way. History's are not while its
    // filter is off, so count them here from the same cache - otherwise the
    // filter list would show a count beside every unticked box but that one. The
    // search narrows this count exactly as it narrows every other chip's: with
    // the chip off its sessions are outside the search scope, so an active query
    // matches none of them - which stays true through a widening, because that
    // folds them in (so the count comes from countByFilter above instead) and
    // resetWidenedSearch prunes their ids back out of the match set.
    if (!foldHistory) {
        counts.history = historyRows.filter(matchesSearch).length;
    }
    renderFilters(counts);

    ensureShell();

    // Nothing grouped at all - no panels to render, so the whole view is one
    // message. Which message it is, emptyStateMarkup decides from the same
    // reasons the settled case below uses.
    const hasAnySession = projects.some((project) => project.sessions.length > 0);
    if (!hasAnySession) {
        heroSlot.replaceChildren();
        noticeSlot.replaceChildren();
        panelsSlot.replaceChildren();
        // Nothing at all in view is where the window matters most: with the
        // chip on, it means nothing fell inside it - not that there is nothing
        // to find. An active query still gets the search wording and its
        // widening offer here: "no agents at all" is not the answer to "where is
        // that text", and this is the very case where it was a past session.
        stateSlot.innerHTML = logic.emptyStateMarkup(emptyStateView(searchActive, state.labels.empty_state, loadingNote));
        syncOpenMenu();
        syncProcPanel();
        syncTooltip();
        return;
    }

    // The banner shows only sessions genuinely blocked on a dialog (question,
    // plan review, permission) that also match the search - the ones that need
    // feedback to continue.
    const blocked = [];
    for (const project of projects) {
        for (const session of project.sessions) {
            if (session.status === 'awaiting_permission' && matchesSearch(session)) {
                blocked.push({ session, projectName: project.name });
            }
        }
    }
    if (blocked.length > 0 && state.filters.has('needs')) {
        setHero(blocked);
    } else {
        heroSlot.replaceChildren();
    }

    // Counted while selecting, not measured afterwards: it is exactly the rows
    // that are on screen against their chip, which is what the notice reports.
    let outsideFilters = 0;
    const visible = [];
    for (const project of projects) {
        const sessions = [];
        for (const session of project.sessions) {
            if (matchesFilter(session) && matchesSearch(session)) {
                sessions.push(session);
            } else if (isWidenedHit(session)) {
                sessions.push(session);
                outsideFilters += 1;
            }
        }
        if (sessions.length === 0) {
            continue;
        }
        // Spread, not a hand-picked subset: the panel's stable identity
        // (key) and its origin fields must survive this projection - losing
        // `key` once gave every panel the reconcile key "undefined", which is
        // the duplicate-key case where unmatched panels accumulate forever.
        visible.push({ ...project, sessions: sortSessions(sessions) });
    }

    // With rows on screen that the chips say are hidden, the list no longer
    // follows the chips - so say so above it, or the extra rows read as a filter
    // that quietly stopped working. The reset sits in the same box: the box is
    // where someone asks "why are these here", so the way back belongs next to
    // the answer - and without it, undoing the widening means retyping the very
    // query that led to it.
    noticeSlot.innerHTML = outsideFilters > 0 ? logic.widenedNoticeMarkup(outsideFilters, state.labels) : '';

    const ordered = logic.sortProjects(visible, state.priorityOrder);

    reconcile(panelsSlot, ordered, (project) => project.key, createPanel, updatePanel);
    // A live scan shows its own note (results keep filling in beneath it); then
    // the history-loading note; then, once settled and with nothing left in
    // view, whichever empty message fits the reason.
    if (visible.length > 0 && !(searchActive && state.searchLoading) && !loadingNote) {
        stateSlot.innerHTML = '';
    } else {
        stateSlot.innerHTML = logic.emptyStateMarkup(
            emptyStateView(searchActive, state.labels.empty_filter || state.labels.empty_state, loadingNote)
        );
    }

    // The CLI-version column is only worth its width when more than one version
    // is in view - the class is what adds its grid track, so with every session
    // on the same version the column is absent rather than repetitive. Set before
    // measuring, so alignColumns sees the layout it is measuring for.
    const shownSessions = visible.flatMap((project) => project.sessions);
    document.body.classList.toggle('show-cli', logic.cliColumnRelevant(shownSessions));

    alignColumns();
    syncOpenMenu();
    syncProcPanel();
    syncTooltip();
}

function renderLoading() {
    document.getElementById('content').innerHTML =
        '<div class="loading">'
        + '<div class="skeleton-panel"></div>'
        + '<div class="skeleton-panel short"></div>'
        + '<div class="skeleton-panel short"></div>'
        + '</div>';
}
