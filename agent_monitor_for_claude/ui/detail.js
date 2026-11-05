'use strict';

/* --- the detail pane: everything about the selected session ---

   The list says what each session is doing; this pane holds the rest, for the
   one session selected - the status with its reasons, the facts, the actions,
   the figures and the blocks for processes, subagents, cost and the session
   itself. A subagent selected in the graph gets the same treatment of its own.
   Nothing here is a tooltip-only value: a tooltip explains a term, the data is
   on the page. Each part is rewritten only when its markup changed, so a
   selection or a hovered tooltip survives the poll. */

// Whether the selected session has a transcript and a scratchpad on disk -
// checked on demand when the selection changes (two quick stats), never on the
// poll. `key` is the session they were checked for.
const detailPaths = { key: null, isNew: false, transcript: '', scratchpad: '' };

const DETAIL_BLOCKS = ['now', 'hits', 'proc', 'plan', 'git', 'agents', 'cost', 'session'];
// A block that spans the pane's width instead of sharing a row: the live ones,
// and a subagent's task and result, which read top to bottom.
const WIDE_BLOCKS = new Set(['now', 'hits', 'proc', 'task', 'result', 'activity']);

// The selected session's uncommitted changes, asked of git on selection and
// again every GIT_REFRESH_MS while it stays selected - never on the poll.
const GIT_REFRESH_MS = 5000;
const gitState = { key: null, result: null, at: 0, pending: false };

// The selected session's current call and last reply, whole: the snapshot cuts
// them to a display length, so the details ask for the whole text whenever the
// cut one changes. `sig` is the cut text the answer belongs to.
const sessionTexts = { key: null, sig: null, result: null, pending: false };

// The task and the result of the selected subagent, whole; `sig` says which
// state of a running agent's file they were read from.
const agentTexts = { key: null, sig: null, task: '', result: '', steps: [], pending: false, open: new Set() };

function selectedSession() {
    return state.selectedKey ? (state.sessionsByKey.get(state.selectedKey) || null) : null;
}

function renderDetail() {
    const pane = document.getElementById('detail');
    if (!pane) {
        return;
    }
    const session = selectedSession();
    if (!session) {
        stopProcBlock();
        pane.__shell = false;
        pane.__agentShell = false;
        pane.__processShell = false;
        setMarkup(pane, '<div class="detail-empty">' + esc(state.labels.detail_empty || '') + '</div>');
        return;
    }
    const agent = selectedAgentOf(session);
    if (agent) {
        renderAgentDetail(pane, session, agent);
        return;
    }
    const process = selectedProcessOf(session);
    if (process) {
        renderProcessDetail(pane, session, process);
        return;
    }

    ensureDetailShell(pane);
    requestDetailPaths(session);
    requestPriority(session);
    requestSessionTexts(session);

    setMarkup(pane.querySelector('.detail-head'), detailHeadMarkup(session));
    updateDetailAge(pane.querySelector('.detail-head .age'), session);
    requestGit(session);
    setMarkup(pane.querySelector('.detail-kpis'), detailKpisMarkup(session));
    const now = nowBlockContent(session);
    updateBlock(pane.querySelector('.block-now'), now.title, '', now.body, now.markdown ? markdownToggleMarkup() : '');
    fillTicks(pane.querySelector('.block-now'));
    updateHitsBlock(pane.querySelector('.block-hits'), session);
    updateProcBlock(pane.querySelector('.block-proc'), session);
    updateBlock(pane.querySelector('.block-plan'), state.labels.block_plan,
        session.plan ? session.plan.step + ' / ' + session.plan.total : '', planBodyMarkup(session));
    const git = gitState.key === rowKey(session) ? gitState.result : null;
    updateBlock(pane.querySelector('.block-git'), state.labels.block_git, gitNote(git), gitBodyMarkup(git));
    updateBlock(pane.querySelector('.block-agents'), state.labels.block_agents, '', agentsBodyMarkup(session));
    updateBlock(pane.querySelector('.block-cost'), state.labels.block_cost, session.usage_compact || '', costBodyMarkup(session));
    updateBlock(pane.querySelector('.block-session'), state.labels.block_session, '', sessionBodyMarkup(session));
    applyShares(pane);
}

// The fixed frame the parts are written into, built once.
function ensureDetailShell(pane) {
    if (pane.__shell) {
        return;
    }
    pane.__shell = true;
    pane.__agentShell = false;
    pane.__processShell = false;
    pane.__markup = null;
    buildPane(pane, DETAIL_BLOCKS);
}

// The frame of a pane: the narrow layout's way back, the head, the figures and
// the named blocks, each hidden until it has something to show.
function buildPane(pane, blockNames) {
    const blocks = blockNames.map((name) => '<section'
        + attr('class', 'block block-' + name + (WIDE_BLOCKS.has(name) ? ' wide' : ''))
        + ' hidden><div class="block-head"><span class="block-title"></span><span class="block-note"></span><span class="block-tools"></span></div>'
        + '<div class="block-body"></div></section>').join('');
    pane.innerHTML = '<button class="detail-back" type="button" data-action="back">&#8592; <span></span> <span class="kbd">Esc</span></button>'
        + '<div class="detail-head"></div>'
        + '<div class="detail-kpis"></div>'
        + '<div class="detail-blocks">' + blocks + '</div>';
    pane.querySelector('.detail-back span').textContent = state.labels.detail_back || '';
}

// Fetch whether the transcript and the scratchpad exist when the selection
// moves to another session, and again once a new session wrote its first
// entry; the actions appear once the answer is in.
function requestDetailPaths(session) {
    const key = rowKey(session);
    const isNew = session.status === 'new';
    if (detailPaths.key === key && detailPaths.isNew === isNew) {
        return;
    }
    if (detailPaths.key !== key) {
        detailPaths.transcript = '';
        detailPaths.scratchpad = '';
    }
    detailPaths.key = key;
    detailPaths.isNew = isNew;
    const origin = session.origin || 'local';
    Promise.all([
        transcriptPath(session.session_id || '', session.cwd || '', origin),
        scratchpadPath(session.session_id || '', session.cwd || '', origin),
    ]).then(([transcript, scratchpad]) => {
        if (detailPaths.key !== key) {
            return;
        }
        detailPaths.transcript = transcript;
        detailPaths.scratchpad = scratchpad;
        renderDetail();
    });
}

// The session on screen is the one someone waits for: while its figures are
// still being read in the background, move its scans to the front - once per
// selection, the queue keeps the order until they ran.
let prioritizedKey = null;

function requestPriority(session) {
    const key = rowKey(session);
    if (!session.scan_pending || session.is_history || prioritizedKey === key) {
        return;
    }
    prioritizedKey = key;
    const bridge = apiBridge();
    if (bridge && typeof bridge.prioritize_session === 'function') {
        logic.settleCall(() => bridge.prioritize_session(session.session_id || '', session.cwd || '', session.origin || 'local'));
    }
}

// Ask git about the selected session's directory when the selection moves, and
// again once the last answer is GIT_REFRESH_MS old.
function requestGit(session) {
    const key = rowKey(session);
    const bridge = apiBridge();
    if (!bridge && window.__MOCK_GIT__) {
        // The browser preview's fabricated answers (dev-mock.js).
        gitState.key = key;
        gitState.result = window.__MOCK_GIT__[session.session_id] || null;
        return;
    }
    if (!bridge || typeof bridge.get_git_status !== 'function' || !session.session_id || !session.cwd) {
        return;
    }
    if (gitState.key !== key) {
        gitState.key = key;
        gitState.result = null;
        gitState.at = 0;
        gitState.pending = false;
    }
    if (gitState.pending || Date.now() - gitState.at < GIT_REFRESH_MS) {
        return;
    }
    gitState.pending = true;
    const worktree = session.worktree_cwd || '';
    const call = () => bridge.get_git_status(session.session_id, session.cwd, worktree, session.origin || 'local');
    Promise.resolve(logic.settleCall(call)).then((result) => {
        if (gitState.key !== key) {
            return;
        }
        gitState.pending = false;
        gitState.at = Date.now();
        gitState.result = result && typeof result === 'object' ? result : null;
        renderDetail();
    });
}

// The selected session's current call and last reply, whole, once the cut ones
// change; a session with neither has nothing to ask for.
function textsSignature(session) {
    const call = session.current_tool;
    return JSON.stringify([call ? call.tool : '', call ? call.text : '', call ? call.questions.length : 0, session.last_reply || '']);
}

function requestSessionTexts(session) {
    const key = rowKey(session);
    const sig = textsSignature(session);
    if ((sessionTexts.key === key && sessionTexts.sig === sig) || sessionTexts.pending) {
        return;
    }
    const bridge = apiBridge();
    if (!bridge || typeof bridge.get_session_texts !== 'function' || session.is_history || !session.session_id || !session.cwd
        || (!session.current_tool && !session.last_reply)) {
        sessionTexts.key = key;
        sessionTexts.sig = sig;
        sessionTexts.result = null;
        return;
    }
    sessionTexts.pending = true;
    const call = () => bridge.get_session_texts(session.session_id, session.cwd, session.origin || 'local');
    Promise.resolve(logic.settleCall(call)).then((result) => {
        sessionTexts.pending = false;
        sessionTexts.key = key;
        sessionTexts.sig = sig;
        sessionTexts.result = result && typeof result === 'object' ? result : null;
        // A selection or a text that moved on meanwhile asks again from here.
        renderDetail();
    });
}

// The call and reply the now block shows: the whole ones when they answer for
// what the snapshot holds, else the cut ones.
function wholeTexts(session) {
    const fresh = sessionTexts.key === rowKey(session) && sessionTexts.sig === textsSignature(session) ? sessionTexts.result : null;
    let call = session.current_tool;
    if (fresh && call) {
        const whole = logic.currentToolOf({ current_tool: fresh.current_tool, current_tool_age_seconds: call.age_seconds });
        if (whole && whole.tool === call.tool) {
            call = whole;
        }
    }
    const reply = fresh && typeof fresh.last_reply === 'string' && fresh.last_reply ? fresh.last_reply : session.last_reply;
    return { call: call, reply: reply };
}

/* --- head: where, what, status, facts, actions --- */

function detailHeadMarkup(session) {
    const labels = state.labels;
    let crumb = '';
    if (session.project_cwd) {
        crumb += '<span class="path-open"' + attr('data-cwd', session.project_cwd) + attr('data-origin', session.origin || 'local')
            + attr('data-tip', labels.show_in_file_manager || '') + '>' + esc(session.project_cwd) + '</span>';
    }
    if (session.project_origin_display) {
        crumb += '<span>' + esc(session.project_origin_display) + '</span>';
    }

    const lines = String(session.status_tip || '').split('\n').slice(1).filter(Boolean);
    const [beforeAge, afterAge] = String(labels.last_activity || '{age}').split('{age}');
    const age = session.age_seconds != null
        ? '<span class="status-age">' + esc(beforeAge) + '<b class="age"></b>' + esc(afterAge || '') + '</span>'
        : '<span class="status-age">' + esc(labels.no_activity || '') + '</span>';

    return '<div class="detail-crumb">' + crumb + '</div>'
        + '<h2 class="detail-title">' + esc(session.name || '') + '</h2>'
        + '<div class="detail-status">'
        +     '<span' + attr('class', 'chip status-' + session.status) + '><i></i><span class="chip-label">' + esc(session.status_label || '') + '</span></span>'
        +     (lines.length > 0 ? '<span class="status-lines">' + lines.map((line) => '<span>' + esc(line) + '</span>').join('') + '</span>' : '')
        +     age
        + '</div>'
        + '<div class="detail-facts">' + factsMarkup(session) + '</div>'
        + '<div class="detail-actions">' + actionsMarkup(session) + '</div>';
}

// The age is ticked by tickAges, so it is set outside the markup - otherwise
// every poll would rewrite the whole head and drop a selection in it.
function updateDetailAge(el, session) {
    if (!el) {
        return;
    }
    const ageAt = session.is_history ? state.historyReceivedAt : state.receivedAt;
    el.dataset.age = session.age_seconds;
    el.dataset.ageAt = ageAt != null ? ageAt : Date.now();
    el.textContent = logic.formatAgeSince(session.age_seconds, Number(el.dataset.ageAt), Date.now(), state.labels);
}

function fact(key, valueMarkup, tip) {
    if (!valueMarkup) {
        return '';
    }
    return '<span' + (tip ? attr('data-tip', tip) : '') + '><span class="fact-key">' + esc(key) + '</span>' + valueMarkup + '</span>';
}

function factsMarkup(session) {
    const labels = state.labels;
    const parts = [];

    const host = hostText(session);
    parts.push(fact(labels.fact_host, host ? esc(host) : '',
        session.wsl ? fmt(labels.host_wsl_tip, { distro: session.origin_label || 'WSL' }) : ''));
    parts.push(fact(labels.fact_model, session.model ? esc(session.model) : ''));
    parts.push(fact(labels.fact_effort, session.effort ? esc(session.effort) : ''));
    parts.push(fact(labels.fact_speed, session.fast_mode ? esc(labels.speed_fast || '') : ''));

    let mode = session.mode ? esc(session.mode) : '';
    if (session.auto_paused) {
        mode += ' <span class="fact-flag">' + esc(labels.signal_auto_paused || '') + '</span>';
    }
    const pausedTip = session.auto_paused
        ? [labels.auto_paused, session.auto_denials_total > 0 ? fmt(labels.auto_denials, { count: session.auto_denials_total }) : '']
            .filter(Boolean).join('\n')
        : '';
    parts.push(fact(labels.fact_mode, mode, pausedTip));

    parts.push(fact(labels.fact_cli, cliMarkup(session), session.cli_update ? fmt(labels.cli_update_tip, session.cli_update) : ''));

    if (session.remote_control) {
        parts.push('<span class="fact-flag"' + attr('data-tip', labels.remote_control_tip || '') + '>'
            + esc(labels.remote_control_on || '') + '</span>');
    }
    return parts.join('');
}

// The Claude Code version, linked to its section of the public changelog. The
// application requests nothing from that address; target="_blank" hands it to
// the host, which opens the system browser - a same-window navigation would
// replace the app's own page. A version that is not a plain release number
// yields no URL and stays text, so the href holds digits and dots only.
function cliMarkup(session) {
    const version = session.cli_version || '';
    if (!version) {
        return '';
    }
    const url = logic.changelogUrl(version);
    let html = url
        ? '<a' + attr('href', url) + ' target="_blank" rel="noreferrer"'
            + attr('data-tip', fmt(state.labels.cli_changelog_tip, { version: version })) + '>' + esc(version) + '</a>'
        : esc(version);
    if (session.cli_update) {
        html += ' <span class="fact-flag">' + esc(fmt(state.labels.signal_update, { version: session.cli_update.available })) + '</span>';
    }
    return html;
}

function actionButton(action, label, options) {
    const opts = options || {};
    return '<button' + attr('type', 'button') + attr('class', 'btn' + (opts.kind ? ' ' + opts.kind : '')) + attr('data-action', action) + '>'
        + esc(label || '') + (opts.key ? ' <span class="kbd">' + esc(opts.key) + '</span>' : '') + '</button>';
}

function actionsMarkup(session) {
    const labels = state.labels;
    const ctrl = labels.key_ctrl || 'Ctrl';
    const known = detailPaths.key === rowKey(session);
    let html = '';
    if (hasWindow(session)) {
        html += actionButton('open-window', labels.action_open_window, { kind: 'primary', key: '↵' });
    }
    html += actionButton('copy-id', labels.action_copy_id, { key: ctrl + ' C' });
    if (session.job_id) {
        html += actionButton('copy-attach', labels.action_copy_attach);
    }
    if (known && detailPaths.transcript) {
        html += actionButton('transcript', labels.action_transcript, { key: 'T' });
    }
    if (known && detailPaths.scratchpad) {
        html += actionButton('scratchpad', labels.action_scratchpad);
    }
    // Only a past session may be deleted - never one with a live process.
    if (session.is_history) {
        html += actionButton('delete', labels.action_delete, { kind: 'danger' });
    }
    return html;
}

/* --- figures --- */

// A share bar; its width is applied by applyShares, since the page policy
// forbids inline styles in markup.
function meterMarkup(ratio, tone) {
    const share = Math.max(0, Math.min(1, Number(ratio) || 0));
    return '<span' + attr('class', 'meter' + (tone ? ' ' + tone : '')) + '><span' + attr('data-share', share) + '></span></span>';
}

function kpiMarkup(label, value, sub, extra, warn) {
    return '<div' + attr('class', 'kpi' + (warn ? ' warn' : '')) + '>'
        + '<span class="kpi-label">' + esc(label || '') + '</span>'
        + '<span class="kpi-value">' + value + '</span>'
        + (extra || '')
        + (sub ? '<span class="kpi-sub">' + esc(sub) + '</span>' : '')
        + '</div>';
}

// The figures, in a fixed order and only those the session has data for.
function detailKpisMarkup(session) {
    const labels = state.labels;
    const kpis = [];

    if (session.usage_compact) {
        const priced = session.cost_usd != null;
        let extra = '';
        let sub = String(session.usage_detail || '').replace(/ · $/, '');
        if (session.cost_shares) {
            extra = '<span class="stack">'
                + '<span class="part-main"' + attr('data-grow', session.cost_shares.main) + '></span>'
                + '<span class="part-agents"' + attr('data-grow', session.cost_shares.agents) + '></span></span>';
            sub = fmt(labels.cost_agents, { cost: logic.formatCost(session.cost_shares.agents) });
        }
        kpis.push(kpiMarkup(priced ? labels.kpi_cost : labels.kpi_tokens, esc(session.usage_compact), sub, extra));
    }

    if (session.context) {
        const ratio = session.context.ratio;
        const sub = ratio === null ? '' : fmt(labels.kpi_context_of, {
            percent: Math.round(ratio * 100), size: logic.formatTokens(session.context.size),
        });
        kpis.push(kpiMarkup(labels.kpi_context, esc(logic.formatTokens(session.context.used)), sub,
            ratio === null ? '' : meterMarkup(ratio, session.context_warn ? 'warn' : ''), session.context_warn));
    }

    if (session.processes > 0) {
        kpis.push(kpiMarkup(labels.kpi_processes, esc(session.processes), ''));
    }

    if (session.workflow_active || session.subagents_running > 0) {
        const value = session.workflow_active ? session.workflow_total : session.subagents_running;
        const sub = session.workflow_active
            ? fmt(labels.subagents_workflow, { done: session.workflow_done, total: session.workflow_total })
            : fmt(labels.subagents_running, { count: session.subagents_running });
        const extra = session.workflow_active && session.workflow_total > 0
            ? meterMarkup(session.workflow_done / session.workflow_total, 'background') : '';
        kpis.push(kpiMarkup(labels.kpi_agents, esc(value), sub, extra));
    }

    if (session.plan) {
        let steps = '<span class="steps">';
        for (let index = 1; index <= session.plan.total; index += 1) {
            const cls = index < session.plan.step ? 'done' : (index === session.plan.step ? 'current' : '');
            steps += '<span' + (cls ? attr('class', cls) : '') + '></span>';
        }
        steps += '</span>';
        kpis.push(kpiMarkup(labels.kpi_plan, esc(session.plan.step + ' / ' + session.plan.total), fmt(labels.plan_tip, session.plan), steps));
    }

    if (session.budget) {
        const warn = session.budget.ratio >= logic.NEAR_LIMIT_RATIO;
        kpis.push(kpiMarkup(labels.kpi_budget, esc(logic.formatCost(session.budget.used)),
            fmt(labels.budget_tip, { used: logic.formatCost(session.budget.used), total: logic.formatCost(session.budget.total) }),
            meterMarkup(session.budget.ratio, warn ? 'warn' : ''), warn));
    }

    if (session.cache) {
        kpis.push(kpiMarkup(labels.kpi_cache, esc(session.cache.warm ? labels.kpi_cache_warm : labels.kpi_cache_cold),
            logic.cacheLine(session.cache, labels), '', session.cache_warn));
    }

    if (session.lines) {
        const value = '<span class="lines-added">+' + esc(session.lines.added) + '</span> '
            + '<span class="lines-removed">−' + esc(session.lines.removed) + '</span>';
        kpis.push(kpiMarkup(labels.kpi_lines, value, session.files_edited > 0 ? fmt(labels.kpi_files_sub, { count: session.files_edited }) : ''));
    }

    if (session.work) {
        const segments = session.work.segments
            .map((segment) => '<span' + attr('data-left', segment.left) + attr('data-share', segment.width) + '></span>').join('');
        const sub = fmt(labels.kpi_work_sub, { wait: logic.formatAge(session.work.wait_seconds, labels), turns: session.work.turns });
        const tip = fmt(labels.kpi_work_tip, { span: logic.formatAge(session.work.span_seconds, labels) });
        kpis.push(kpiMarkup(labels.kpi_work, esc(logic.formatAge(session.work.work_seconds, labels)), sub,
            '<span class="timeline"' + attr('data-tip', tip) + '>' + segments + '</span>'));
    }
    return kpis.join('');
}

// Size the share bars and the stacked cost bar from their data attributes.
function applyShares(root) {
    root.querySelectorAll('[data-share]').forEach((el) => {
        el.style.width = (Number(el.dataset.share) * 100).toFixed(1) + '%';
    });
    root.querySelectorAll('[data-left]').forEach((el) => {
        el.style.left = (Number(el.dataset.left) * 100).toFixed(2) + '%';
    });
    root.querySelectorAll('[data-grow]').forEach((el) => {
        el.style.flexGrow = String(Math.max(0, Number(el.dataset.grow) || 0));
    });
}

/* --- blocks --- */

// What the session waits on (a command, a question with its answers, a plan
// to review), what it runs right now, or what it said last.
function nowBlockContent(session) {
    const labels = state.labels;
    const { call, reply } = wholeTexts(session);
    const waiting = session.status === 'awaiting_permission';
    if (call && (waiting || session.status === 'working' || session.status === 'processing')) {
        const subject = logic.SUBAGENT_TOOLS.has(call.tool) && call.text ? call.name + ' · ' + call.text : call.name;
        let body = '<div class="now-call"><div class="now-tool"><b>' + esc(subject) + '</b>'
            + (call.detail && !call.plan ? '<span>' + esc(call.detail) + '</span>' : '')
            + (!waiting && call.age_seconds != null ? tickMarkup(call.age_seconds, state.receivedAt) : '')
            + '</div>';
        for (const question of call.questions) {
            const options = question.options.map((option) => '<span class="option">' + esc(option) + '</span>').join('');
            body += '<div class="question"><span>' + esc(question.text) + '</span>'
                + (options ? '<span class="options">' + options + '</span>' : '') + '</div>';
        }
        // A subagent call names its type in the line above; only a command, a path or a plan earns a block.
        // A plan is Markdown; a command or a path is not.
        if (call.text && call.questions.length === 0 && !logic.SUBAGENT_TOOLS.has(call.tool)) {
            body += call.plan ? textMarkup(call.text, true) : '<pre class="now-text mono">' + esc(call.text) + '</pre>';
        }
        body += '</div>';
        return { title: waiting ? labels.block_waiting : labels.block_running, body: body, markdown: Boolean(call.plan && call.text) };
    }
    if (reply && !waiting) {
        return { title: labels.block_reply, body: textMarkup(reply, true), markdown: true };
    }
    return { title: '', body: '', markdown: false };
}

// A text the details show whole: Markdown rendered while the formatted view is
// on, as written otherwise. Both are a .now-text, so a block holding nothing
// else is that text edge to edge.
function textMarkup(text, markdown) {
    if (markdown && state.markdown) {
        return '<div class="now-text md">' + logic.markdownToHtml(text) + '</div>';
    }
    return '<pre class="now-text">' + esc(text) + '</pre>';
}

// The switch between the formatted and the written text, in the head of every
// block that shows Markdown. One choice for all of them, remembered.
function markdownToggleMarkup() {
    const labels = state.labels;
    const button = (mode, label) => {
        const active = (mode === 'formatted') === state.markdown;
        return '<button' + attr('type', 'button') + attr('class', 'seg' + (active ? ' active' : '')) + attr('data-action', 'markdown')
            + attr('data-mode', mode) + attr('aria-pressed', active ? 'true' : 'false') + '>' + esc(label || '') + '</button>';
    };
    return '<span class="md-toggle">' + button('formatted', labels.md_formatted) + button('plain', labels.md_plain) + '</span>';
}

function setMarkdown(formatted) {
    state.markdown = formatted;
    try {
        localStorage.setItem('amc-markdown', formatted ? 'formatted' : 'plain');
    } catch (e) { /* storage unavailable */ }
    renderDetail();
}

function planBodyMarkup(session) {
    if (session.plan_items.length === 0) {
        return '';
    }
    return '<ol class="plan-list">'
        + session.plan_items.map((item) => '<li' + attr('class', item.status) + '>' + esc(item.text) + '</li>').join('')
        + '</ol>';
}

function gitNote(git) {
    if (!git || typeof git.branch !== 'string' || !git.branch) {
        return '';
    }
    const labels = state.labels;
    const parts = [git.branch];
    if (Number.isInteger(git.ahead) && git.ahead > 0) {
        parts.push(fmt(labels.git_ahead, { count: git.ahead }));
    }
    if (Number.isInteger(git.behind) && git.behind > 0) {
        parts.push(fmt(labels.git_behind, { count: git.behind }));
    }
    return parts.join(' · ');
}

// The working directory's uncommitted files and lines, and how many of the
// files this session edited are among them.
function gitBodyMarkup(git) {
    if (!git) {
        return '';
    }
    const labels = state.labels;
    const count = (value) => (Number.isInteger(value) && value > 0 ? value : 0);
    const plusMinus = (added, removed) => '<span class="lines-added">+' + esc(count(added)) + '</span> '
        + '<span class="lines-removed">−' + esc(count(removed)) + '</span>';
    const changed = count(git.changed);
    const untracked = count(git.untracked);
    if (changed === 0 && untracked === 0) {
        return '<div class="line-list"><div>' + esc(labels.git_clean || '') + '</div></div>';
    }
    let html = '<div class="line-list"><div>' + esc(fmt(labels.git_summary, { changed: changed, untracked: untracked }))
        + ' · ' + plusMinus(git.added, git.removed) + '</div>';
    if (count(git.edited_total) > 0) {
        html += '<div>' + esc(fmt(labels.git_edited, { uncommitted: count(git.edited_uncommitted), total: count(git.edited_total) })) + '</div>';
    }
    html += '</div>';
    const files = Array.isArray(git.files) ? git.files : [];
    if (files.length > 0) {
        html += '<div class="git-files">' + files.map((file) => {
            const edited = file.edited === true;
            const lines = Number.isInteger(file.added) || Number.isInteger(file.removed) ? plusMinus(file.added, file.removed) : '';
            return '<span class="git-code">' + esc(typeof file.status === 'string' ? file.status : '') + '</span>'
                + '<span' + attr('class', 'git-path' + (edited ? ' edited' : '')) + (edited ? attr('data-tip', labels.git_edited_tip || '') : '') + '>'
                + esc(typeof file.path === 'string' ? file.path : '') + '</span>'
                + '<span class="git-lines">' + lines + '</span>';
        }).join('') + '</div>';
    }
    return html;
}

function updateBlock(el, title, note, bodyMarkup, toolsMarkup) {
    if (!el) {
        return;
    }
    el.hidden = !bodyMarkup;
    if (!bodyMarkup) {
        return;
    }
    setText(el.querySelector('.block-title'), title);
    setText(el.querySelector('.block-note'), note);
    setMarkup(el.querySelector('.block-tools'), toolsMarkup || '');
    setMarkup(el.querySelector('.block-body'), bodyMarkup);
}

// Every passage the content search found here. Rewritten only for a different
// result object, so a selection inside survives the poll.
function updateHitsBlock(el, session) {
    const hit = searchHitFor(session.session_id);
    el.hidden = !hit;
    if (!hit) {
        return;
    }
    setText(el.querySelector('.block-title'), state.labels.block_hits);
    const body = el.querySelector('.block-body');
    if (body.__searchHit !== hit) {
        body.__searchHit = hit;
        body.innerHTML = logic.searchHitsMarkup(hit, state.labels);
    }
}

// The live processes and tasks, polled only while this block is on screen.
function updateProcBlock(el, session) {
    const show = session.processes > 0 && !session.is_history;
    el.hidden = !show;
    if (!show) {
        stopProcBlock();
        return;
    }
    setText(el.querySelector('.block-title'), state.labels.block_processes);
    showProcBlock(el.querySelector('.block-body'), el.querySelector('.block-note'), session);
}

function agentsBodyMarkup(session) {
    const labels = state.labels;
    const lines = [];
    if (session.workflow_active) {
        lines.push('<div>' + esc(fmt(labels.subagents_workflow, { done: session.workflow_done, total: session.workflow_total })) + '</div>');
    }
    if (session.subagents_running > 0) {
        lines.push('<div>' + esc(fmt(labels.subagents_running, { count: session.subagents_running })) + '</div>');
    }
    for (const label of session.subagents_labels || []) {
        lines.push('<div class="agent-line"><i></i><span>' + esc(label) + '</span></div>');
    }
    // "Recently finished" counts the agent files of the recent window; for a
    // workflow the done/total line above already says it.
    if (!session.workflow_active && session.subagents_done > 0) {
        lines.push('<div class="line-sub">' + esc(fmt(labels.subagents_finished, { count: session.subagents_done })) + '</div>');
    }
    return lines.length > 0 ? '<div class="line-list">' + lines.join('') + '</div>' : '';
}

// How the cost divides (main conversation, subagents, skills, web searches),
// and the prompt cache's outlook.
function costBodyMarkup(session) {
    const labels = state.labels;
    const lines = String(session.cost_tip || '').split('\n').filter(Boolean);
    if (lines.length === 0) {
        return '';
    }
    return '<div class="line-list">' + lines.map((line) => line.startsWith('• ')
        ? '<div class="line-sub">' + esc(line.slice(2)) + '</div>'
        : '<div>' + esc(line) + '</div>').join('') + '</div>';
}

function kvRow(key, valueMarkup) {
    return valueMarkup ? '<span class="kv-key">' + esc(key) + '</span><span class="kv-value">' + valueMarkup + '</span>' : '';
}

// One row per failing hook - its name, exit code and how often it failed -
// and the first line of its error output under it when it wrote one.
function hookErrorsMarkup(errors) {
    const labels = state.labels;
    return '<span class="hook-list">' + errors.map((error) => '<span class="hook-name">' + esc(error.hook) + '</span>'
        + '<span class="hook-code">' + esc(error.exit_code === null ? '' : fmt(labels.hook_exit_code, { code: error.exit_code })) + '</span>'
        + '<span class="hook-count">' + esc(error.count + '×') + '</span>'
        + (error.message ? '<span class="hook-message">' + esc(error.message) + '</span>' : '')).join('') + '</span>';
}

function sessionBodyMarkup(session) {
    const labels = state.labels;
    let kv = kvRow(labels.session_id_label, '<span class="mono">' + esc(session.session_id || '') + '</span>');
    kv += kvRow(labels.fact_branch, session.branch ? esc(session.branch) : '');
    if (session.worktree) {
        kv += kvRow(labels.fact_worktree, esc(session.worktree.path)
            + (session.worktree.branch ? ' · ' + esc(session.worktree.branch) : ''));
    }
    if (session.pull_requests.length > 0) {
        kv += kvRow(labels.pr_label, session.pull_requests.map((pr) => {
            const text = esc(pr.repository + ' #' + pr.number);
            // Like the changelog link: the host opens the address in the system
            // browser; one that failed logic.pullRequestUrl is listed as text.
            return pr.url ? '<a' + attr('href', pr.url) + ' target="_blank" rel="noreferrer">' + text + '</a>' : text;
        }).join('<br>'));
    }
    if (session.artifact_links.length > 0) {
        // Opened by the host in the system browser, like a pull request.
        kv += kvRow(labels.artifacts_label, session.artifact_links.map((artifact) => (artifact.url
            ? '<a' + attr('href', artifact.url) + ' target="_blank" rel="noreferrer">' + esc(artifact.title) + '</a>'
            : esc(artifact.title))).join('<br>'));
    }
    if (session.hook_errors.length > 0) {
        kv += kvRow(labels.hook_errors_label, hookErrorsMarkup(session.hook_errors));
    }
    if (session.model_switched) {
        kv += kvRow(labels.model_history_label, session.model_history
            .map((entry) => esc(fmtDateTime(entry.time) + '  ' + entry.label)).join('<br>'));
    }
    if (session.cli_switched) {
        kv += kvRow(labels.cli_history_label, session.cli_history
            .map((entry) => esc(fmtDateTime(entry.time) + '  ' + entry.version + (entry.origin ? ' · ' + entry.origin : ''))).join('<br>'));
    }

    const counts = [];
    if (session.compactions > 0) {
        counts.push(fmt(labels.compactions, { count: session.compactions }));
    }
    // The count stands in only while the addresses are unknown (the first
    // scan still running, or a transcript that never recorded one).
    if (session.artifacts > 0 && session.artifact_links.length === 0) {
        counts.push(fmt(labels.artifacts, { count: session.artifacts }));
    }
    if (session.queued > 0) {
        counts.push(fmt(labels.queued_tip, { count: session.queued }));
    }
    return '<div class="kv">' + kv + '</div>'
        + (counts.length > 0 ? '<div class="line-list">' + counts.map((line) => '<div>' + esc(line) + '</div>').join('') + '</div>' : '');
}

/* --- a subagent selected in the graph --- */

// The selected subagent of `session`, numbered like the graph numbers it, or
// null - a selection whose agent is no longer listed falls back to the session.
function selectedAgentOf(session) {
    if (!state.selectedAgent) {
        return null;
    }
    const agent = logic.agentsByStart(session.agents || []).find((item) => item.id === state.selectedAgent) || null;
    if (!agent) {
        state.selectedAgent = null;
    }
    return agent;
}

function renderAgentDetail(pane, session, agent) {
    stopProcBlock();
    if (!pane.__agentShell) {
        pane.__agentShell = true;
        pane.__shell = false;
        pane.__processShell = false;
        pane.__markup = null;
        buildPane(pane, ['task', 'result', 'activity']);
    }
    requestAgentTexts(session, agent);

    setMarkup(pane.querySelector('.detail-head'), agentHeadMarkup(session, agent));
    setMarkup(pane.querySelector('.detail-kpis'), agentKpisMarkup(agent));
    const texts = agentTexts.key === agentTextsKey(session, agent) ? agentTexts : { task: '', result: '', steps: [] };
    // A running agent's newest reply is its latest word, not yet its result.
    const resultTitle = agent.running ? state.labels.block_reply : state.labels.block_result;
    updateBlock(pane.querySelector('.block-result'), resultTitle, '', texts.result ? textMarkup(texts.result, true) : '', markdownToggleMarkup());
    updateBlock(pane.querySelector('.block-task'), state.labels.block_task, '', texts.task ? textMarkup(texts.task, true) : '', markdownToggleMarkup());
    // A tool selected under the agent in the graph narrows the steps to its calls.
    const toolKey = state.selectedTool;
    const steps = toolKey ? texts.steps.filter((step) => step.kind === 'call' && logic.toolNodeKey(step.tool) === toolKey) : texts.steps;
    const calls = steps.filter((step) => step.kind === 'call').length;
    const failed = steps.filter((step) => step.outcome === 'failed').length;
    const note = calls > 0
        ? fmt(state.labels.activity_note, { count: calls }) + (failed > 0 ? ' · ' + fmt(state.labels.graph_tool_failed, { count: failed }) : '')
        : '';
    const filter = toolKey
        ? '<button type="button" class="step-filter" data-action="step-filter-clear">'
            + esc(logic.toolNodeLabel(toolKey)) + ' <span aria-hidden="true">×</span></button>'
        : '';
    const activity = pane.querySelector('.block-activity');
    updateBlock(activity, state.labels.block_activity, note, activityMarkup(steps, agent), filter);
    // A tool picked in the graph brings its calls on screen, once they are in.
    if (state.scrollToActivity && activity && !activity.hidden) {
        state.scrollToActivity = false;
        activity.scrollIntoView({ block: 'start' });
    }
    applyShares(pane);
}

function agentTextsKey(session, agent) {
    return rowKey(session) + '/' + agent.id;
}

// The agent's whole task and result, read from its own transcript on
// selection - and again whenever a running agent wrote to it, since its
// newest reply moves on; a finished agent's texts never change.
function requestAgentTexts(session, agent) {
    const key = agentTextsKey(session, agent);
    const lastWrite = agent.age_seconds !== null && state.receivedAt !== null ? Math.round(state.receivedAt / 1000 - agent.age_seconds) : 0;
    const sig = agent.running ? 'running:' + lastWrite : 'finished';
    if ((agentTexts.key === key && agentTexts.sig === sig) || agentTexts.pending) {
        return;
    }
    if (agentTexts.key !== key) {
        agentTexts.task = '';
        agentTexts.result = '';
        agentTexts.steps = [];
        agentTexts.open = new Set();
    }
    agentTexts.key = key;
    agentTexts.sig = sig;
    const bridge = apiBridge();
    if (!bridge || typeof bridge.get_agent_texts !== 'function' || !session.session_id) {
        return;
    }
    agentTexts.pending = true;
    const call = () => bridge.get_agent_texts(session.session_id, agent.id, session.origin || 'local');
    Promise.resolve(logic.settleCall(call)).then((texts) => {
        agentTexts.pending = false;
        if (agentTexts.key !== key) {
            renderDetail();
            return;
        }
        agentTexts.task = texts && typeof texts.task === 'string' ? texts.task : '';
        agentTexts.result = texts && typeof texts.result === 'string' ? texts.result : '';
        agentTexts.steps = logic.agentStepsOf(texts ? texts.steps : null);
        renderDetail();
    });
}

function agentHeadMarkup(session, agent) {
    const labels = state.labels;
    const status = agent.running ? 'working' : 'completed';
    const age = agent.age_seconds !== null
        ? '<span class="status-age">' + esc(fmt(labels.last_activity, { age: logic.formatAge(agent.age_seconds, labels) })) + '</span>'
        : '';
    return '<div class="detail-crumb"><button type="button" class="crumb-link" data-action="agent-session">'
        + esc('← ' + (session.name || '')) + '</button></div>'
        + '<h2 class="detail-title">' + esc('#' + agent.number + ' ' + logic.agentTitle(agent)) + '</h2>'
        + '<div class="detail-status">'
        +     '<span' + attr('class', 'chip status-' + status) + '><i></i><span class="chip-label">'
        +     esc(agent.running ? labels.agent_running : labels.agent_finished) + '</span></span>'
        +     age
        + '</div>'
        + '<div class="detail-facts">' + agentFactsMarkup(session, agent) + '</div>';
}

function agentFactsMarkup(session, agent) {
    const labels = state.labels;
    const launches = new Map([['foreground', labels.launch_foreground], ['background', labels.launch_background], ['skill', labels.launch_skill]]);
    const model = logic.formatModel(agent.model);

    // The agent that started this one, or the session itself.
    const parent = agent.parent_id ? logic.agentsByStart(session.agents || []).find((item) => item.id === agent.parent_id) : null;
    const startedBy = parent
        ? '<button type="button" class="crumb-link"' + attr('data-action', 'agent-parent') + attr('data-agent', parent.id) + '>'
            + esc('#' + parent.number + ' ' + logic.agentTitle(parent)) + '</button>'
        : '<button type="button" class="crumb-link" data-action="agent-session">' + esc(session.name || '') + '</button>';

    return [
        fact(labels.fact_agent_type, agent.type ? esc(agent.type) : ''),
        fact(labels.fact_skill, agent.skill ? esc('/' + agent.skill) : ''),
        fact(labels.fact_model, model ? esc(model) : ''),
        fact(labels.fact_effort, agent.effort ? esc(agent.effort) : ''),
        fact(labels.fact_launch, launches.has(agent.launch) ? esc(launches.get(agent.launch) || '') : ''),
        fact(labels.fact_started_by, startedBy),
    ].join('');
}

// Cost, runtime with the start, tool calls and the lines it changed - each only
// when the agent's scan has it.
function agentKpisMarkup(agent) {
    const labels = state.labels;
    const kpis = [];
    if (agent.cost_usd !== null) {
        kpis.push(kpiMarkup(labels.kpi_cost, esc(logic.formatCost(agent.cost_usd)), ''));
    }
    const runtime = logic.agentRuntime(agent);
    if (runtime !== null) {
        const at = state.receivedAt !== null ? new Date(state.receivedAt - agent.started_seconds * 1000).toISOString() : '';
        kpis.push(kpiMarkup(labels.kpi_runtime, esc(logic.formatAge(runtime, labels)), at ? fmt(labels.kpi_runtime_sub, { time: fmtDateTime(at) }) : ''));
    }
    if (agent.tool_calls > 0) {
        kpis.push(kpiMarkup(labels.kpi_tool_calls, esc(agent.tool_calls), ''));
    }
    if (agent.lines_added > 0 || agent.lines_removed > 0) {
        const value = '<span class="lines-added">+' + esc(agent.lines_added) + '</span> '
            + '<span class="lines-removed">−' + esc(agent.lines_removed) + '</span>';
        kpis.push(kpiMarkup(labels.kpi_lines, value, ''));
    }
    return kpis.join('');
}

/* --- a background process selected in the graph or the process table --- */

// The selected process of `session`, or null - one that ended falls back to the session.
function selectedProcessOf(session) {
    if (!state.selectedProcess) {
        return null;
    }
    const process = (session.process_list || []).find((item) => item.pid === state.selectedProcess) || null;
    if (!process) {
        state.selectedProcess = null;
    }
    return process;
}

// Its name, pid and parent on top, its own live figures under them (filled by
// the process table's poll), then the session's whole table with this row marked.
function renderProcessDetail(pane, session, process) {
    if (!pane.__processShell) {
        stopProcBlock();
        pane.__processShell = true;
        pane.__shell = false;
        pane.__agentShell = false;
        pane.__markup = null;
        buildPane(pane, ['proc']);
    }
    setMarkup(pane.querySelector('.detail-head'), processHeadMarkup(session, process));
    updateProcBlock(pane.querySelector('.block-proc'), session);
}

function processHeadMarkup(session, process) {
    const labels = state.labels;
    const parent = process.parent ? (session.process_list || []).find((item) => item.pid === process.parent) : null;
    const parentMarkup = parent
        ? '<button type="button" class="crumb-link"' + attr('data-action', 'process-parent') + attr('data-process', parent.pid) + '>'
            + esc(parent.name + ' ' + parent.pid) + '</button>'
        : '<button type="button" class="crumb-link" data-action="agent-session">' + esc(session.name || '') + '</button>';
    return '<div class="detail-crumb"><button type="button" class="crumb-link" data-action="agent-session">'
        + esc('← ' + (session.name || '')) + '</button></div>'
        + '<h2 class="detail-title">' + esc(process.name) + '</h2>'
        + '<div class="detail-status"><span class="chip status-working"><i></i><span class="chip-label">'
        + esc(labels.agent_running || '') + '</span></span></div>'
        + '<div class="detail-facts">'
        + fact(labels.fact_pid || 'PID', esc(process.pid))
        + fact(labels.fact_parent_process, parentMarkup)
        + '</div>';
}

// The selected process's CPU, memory and uptime from the process table's
// latest sample; called by that table on every tick.
function updateProcessKpis(stats) {
    const pane = document.getElementById('detail');
    if (!pane || !pane.__processShell || state.selectedProcess === null) {
        return;
    }
    const labels = state.labels;
    const stat = stats.find((item) => item.pid === state.selectedProcess);
    const kpis = stat
        ? kpiMarkup(labels.proc_col_cpu, esc(formatCpu(stat.cpu)), '')
            + kpiMarkup(labels.proc_col_memory, esc(formatBytes(stat.rss)), '')
            + kpiMarkup(labels.proc_col_uptime, esc(formatDurationShort(stat.uptime)), '')
        : '';
    setMarkup(pane.querySelector('.detail-kpis'), kpis);
}

/* --- a subagent's steps: every tool call with its request and result --- */

// One collapsed row per step - when it happened (from the first step), what it
// did and how long it took - opening to the whole request and result. A reply
// between calls is a step too. Which rows are open survives a refresh.
function activityMarkup(steps, agent) {
    if (steps.length === 0) {
        return '';
    }
    const labels = state.labels;
    const start = steps.find((step) => step.at !== null);
    const offset = (at) => (start && at !== null ? '+' + logic.formatClock(Math.max(0, at - start.at)) : '');
    let html = '<div class="activity-list">';
    steps.forEach((step, index) => {
        const id = step.kind === 'call' ? step.id : 'text-' + index;
        const open = agentTexts.open.has(id) ? attr('open', '') : '';
        if (step.kind === 'text') {
            html += '<details class="step step-text"' + attr('data-step', id) + open + '><summary>'
                + '<span class="step-at">' + esc(offset(step.at)) + '</span>'
                + '<span class="step-tool">' + esc(labels.step_reply || '') + '</span>'
                + '<span class="step-subject">' + esc(logic.firstLine(step.text)) + '</span><span></span></summary>'
                + '<div class="step-body">' + textMarkup(step.text, true) + '</div></details>';
            return;
        }
        const running = step.result === null;
        const duration = !running && step.at !== null && step.result_at !== null ? logic.stepDuration(step.result_at - step.at, labels) : '';
        // A failure in red, with its exit status when the command gave one; a
        // call you rejected or one stopped early in yellow - neither failed.
        let flag = '';
        if (step.outcome === 'failed') {
            flag = '<span class="step-flag failed">'
                + esc(step.exit_code !== null ? fmt(labels.step_exit, { code: step.exit_code }) : (labels.step_error || '')) + '</span>';
        } else if (step.outcome === 'rejected' || step.outcome === 'interrupted') {
            flag = '<span class="step-flag declined">' + esc(step.outcome === 'rejected' ? labels.step_rejected : labels.step_interrupted) + '</span>';
        } else if (running) {
            flag = '<span class="step-flag">' + esc(agent.running ? labels.agent_running : '') + '</span>';
        }
        const tone = step.outcome === 'failed' ? ' failed' : (step.outcome ? ' declined' : '');
        html += '<details' + attr('class', 'step' + tone) + attr('data-step', id) + open + '><summary>'
            + '<span class="step-at">' + esc(offset(step.at)) + '</span>'
            + '<span class="step-tool">' + esc(step.tool) + '</span>'
            + '<span class="step-subject">' + esc(logic.stepSubject(step)) + '</span>'
            + '<span class="step-end">' + flag + esc(duration) + '</span></summary>'
            + '<div class="step-body"><div class="step-sub">' + esc(labels.step_request || '') + '</div><div class="step-input">'
            + Object.keys(step.input).map((key) => '<span class="kv-key">' + esc(key) + '</span>'
                + '<pre class="step-value">' + esc(logic.stepInputText(step.input[key])) + '</pre>').join('')
            + '</div>'
            + (running ? '' : '<div class="step-sub">' + esc(labels.block_result || '') + '</div><pre class="step-value step-result">'
                + esc(step.result) + '</pre>')
            + '</div></details>';
    });
    return html + '</div>';
}

// Remember which steps are open, so a refresh of a running agent keeps them.
function onStepToggle(event) {
    const step = event.target;
    if (!step || !step.matches || !step.matches('details[data-step]')) {
        return;
    }
    if (step.open) {
        agentTexts.open.add(step.dataset.step);
    } else {
        agentTexts.open.delete(step.dataset.step);
    }
}
