'use strict';

/* --- content search --- */

// Content search. The query is matched against the transcript CONTENT, which
// only the Python bridge can read; it streams back matching session ids (and
// progress) via window.__amcSearchPush, and the UI shows them as they arrive.
// It searches the open sections only. A query shorter than
// this is treated as no filter, so a single stray character never scans.
const SEARCH_MIN_CHARS = 2;

// Wait for a pause in typing before scanning, so a burst of keystrokes starts
// one search, not one per character.
const SEARCH_DEBOUNCE_MS = 300;

// The editor-style option toggles: element id -> the state flag it drives.
const SEARCH_OPT_BUTTONS = [
    ['opt-case', 'searchMatchCase', 'search_match_case'],
    ['opt-word', 'searchWholeWord', 'search_whole_word'],
    ['opt-regex', 'searchRegex', 'search_regex'],
];

function initSearch() {
    const input = document.getElementById('search-input');
    if (!input) {
        return;
    }

    // Receive streaming results pushed from the Python side. Registered here so
    // it exists before any search can start.
    window.__amcSearchPush = onSearchPush;

    input.value = state.search;

    input.addEventListener('input', () => setSearchQuery(input.value));

    input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && input.value) {
            event.stopPropagation();
            input.value = '';
            setSearchQuery('');
        }
    });

    // Each toggle flips its option, is persisted, and re-runs the search at once
    // (a click is a discrete action, so no debounce).
    SEARCH_OPT_BUTTONS.forEach(([id, key]) => {
        const button = document.getElementById(id);
        if (!button) {
            return;
        }
        button.addEventListener('click', () => {
            state[key] = !state[key];
            persistSearchOptions();
            refreshSearchOptButtons();
            runSearch();
        });
    });

    refreshSearchOptButtons();
}

// Reflect the option flags on their buttons (active state + tooltip). Called on
// init, after each toggle, and once labels arrive (applyBootstrap).
function refreshSearchOptButtons() {
    SEARCH_OPT_BUTTONS.forEach(([id, key, tipKey]) => {
        const button = document.getElementById(id);
        if (!button) {
            return;
        }
        const on = !!state[key];
        button.classList.toggle('active', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
        const tip = state.labels[tipKey];
        if (tip) {
            button.dataset.tip = tip;
            button.setAttribute('aria-label', tip);
        }
    });
}

function persistSearchOptions() {
    try {
        localStorage.setItem('amc-search-opts', JSON.stringify({
            matchCase: state.searchMatchCase,
            wholeWord: state.searchWholeWord,
            regex: state.searchRegex,
        }));
    } catch (e) { /* storage unavailable */ }
}

function searchOptions() {
    return { match_case: state.searchMatchCase, whole_word: state.searchWholeWord, use_regex: state.searchRegex };
}

// Front-end regex validity check for instant red-box feedback. The backend
// (Python `re`) is authoritative and can still flag an error via its push, but
// this catches the common case without a round-trip.
function isValidRegex(pattern) {
    try {
        new RegExp(pattern);
        return true;
    } catch (e) {
        return false;
    }
}

// Toggle the invalid (red) state of the box, and give it a tooltip explaining
// why while invalid.
function updateSearchBox() {
    const box = document.querySelector('.search-box');
    if (box) {
        box.classList.toggle('invalid', !!state.searchError);
    }
    const input = document.getElementById('search-input');
    if (input) {
        if (state.searchError) {
            input.setAttribute('aria-invalid', 'true');
            input.dataset.tip = state.labels.search_regex_invalid || '';
        } else {
            input.removeAttribute('aria-invalid');
            delete input.dataset.tip;
        }
    }
}

// The one place the query text changes. A widened scope answers exactly one
// question, so every edit to the text - typing, clearing, the input's own clear
// button - is a new question and puts the search back to the sessions in view.
// The empty state then offers to widen again if the new query comes up empty.
//
// The session's own fields filter at once (logic.sessionMetaMatch, in render);
// the content scan follows after the typing pause. The previous text's content
// results no longer apply, and the bumped seq makes a scan still reporting for
// that text drop its late pushes.
function setSearchQuery(value) {
    state.search = value;
    state.searchIncludeHidden = false;
    state.searchMatches = null;
    state.searchSeq += 1;
    scheduleSearch();
    if (state.last) {
        render(state.last);
    }
}

function scheduleSearch() {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
}

// The search scope (in logic.searchScopeRefs) is exactly the sessions the active
// filter chips show - not every session - so Python reads only the transcripts
// the user can actually see. The delta rescan (includeHistory false) additionally
// skips dead sessions, which cannot gain a new match.
function collectSearchRefs(includeHistory) {
    return logic.searchScopeRefs(
        state.last ? state.last.sessions : [],
        visibleHistory(),
        state.filters,
        includeHistory,
        state.searchIncludeHidden
    );
}

function currentSessionRefs() {
    return collectSearchRefs(true);
}

// Start (or clear) a content search. Bumps the sequence id so any in-flight scan
// - and any late push from it - is superseded and ignored.
function runSearch() {
    // A direct caller (a filter chip or a search-option toggle) can run this
    // while a debounce timer from recent typing is still pending. Clear it, or it
    // fires afterward and restarts an identical search - dropping the matches
    // already streaming in and restarting the progress bar.
    clearTimeout(state.searchTimer);
    state.searchTimer = null;

    const query = state.search.trim();
    const active = query.length >= SEARCH_MIN_CHARS;
    const seq = ++state.searchSeq;
    const bridge = apiBridge();

    // Regex mode with an invalid pattern: flag the box red at once and do not
    // scan. (The backend re-validates too and can flag it via its push.)
    if (active && state.searchRegex && !isValidRegex(query)) {
        state.searchError = true;
        state.searchMatches = null;
        state.searchLoading = false;
        state.searchProcessed = 0;
        state.searchTotal = 0;
        // Cancel any in-flight backend scan (a prior valid pattern still running):
        // the local seq bump is invisible to Python, so without a fresh
        // start_search the earlier scan reads every remaining transcript to the end.
        if (bridge && typeof bridge.start_search === 'function') {
            logic.settleCall(() => bridge.start_search('', [], searchOptions(), seq));
        }
        updateSearchProgress();
        updateSearchBox();
        if (state.last) {
            render(state.last);
        }
        return;
    }
    state.searchError = false;
    updateSearchBox();

    if (!active) {
        state.searchMatches = null;
        state.searchLoading = false;
        state.searchProcessed = 0;
        state.searchTotal = 0;
        // Cancel a running backend search: the new (unused) seq invalidates it.
        if (bridge && typeof bridge.start_search === 'function') {
            logic.settleCall(() => bridge.start_search('', [], searchOptions(), seq));
        }
        updateSearchProgress();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    state.searchMatches = new Map();
    state.searchLoading = true;
    state.searchProcessed = 0;
    state.searchTotal = 0;
    updateSearchProgress();
    if (state.last) {
        render(state.last);
    }

    if (!bridge || typeof bridge.start_search !== 'function') {
        // Browser preview: no file access without the bridge, so fall back to a
        // synchronous client-side match over the mock records' visible fields.
        state.searchMatches = mockSearchMatches(query);
        state.searchLoading = false;
        updateSearchProgress();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    // Fire-and-forget: a Python-side rejection must not escape to the global
    // handler (which wipes the content area) or leave searchLoading stuck true.
    logic.settleCall(
        () => bridge.start_search(query, currentSessionRefs(), searchOptions(), seq),
        () => {
            state.searchLoading = false;
            updateSearchProgress();
            if (state.last) {
                render(state.last);
            }
        }
    );
}

// Refresh an active search when the snapshot changes, so newly-appearing matches
// show up on their own. Transcripts are append-only: an existing match never
// stops matching, and a NEW match can only appear in a live session that has
// grown. So the delta scope is just the chip-visible live sessions not already
// matched - cheap even with history on (dead history is never re-read, matched
// sessions are skipped). Runs silently (no progress bar): the new matches simply
// stream in and are ADDED to the set, so nothing already shown flickers away.
function rescanForNewMatches() {
    const query = state.search.trim();
    if (query.length < SEARCH_MIN_CHARS || state.searchError || state.searchLoading) {
        return;
    }
    if (!(state.searchMatches instanceof Map)) {
        return;
    }

    const bridge = apiBridge();
    if (!bridge || typeof bridge.start_search !== 'function') {
        return;
    }

    const refs = collectSearchRefs(false).filter((ref) => !state.searchMatches.has(ref.session_id));
    if (refs.length === 0) {
        return;
    }

    // A fresh seq so any late push from a prior scan is ignored; matches are
    // added to the existing set (never reset), so results only grow.
    const seq = ++state.searchSeq;
    // Fire-and-forget; a bridge hiccup (sync or async) is contained - the next
    // change retries - and never reaches the global handler.
    logic.settleCall(() => bridge.start_search(query, refs, searchOptions(), seq));
}

// Take the empty result's offer to look wider. Deliberately one step per click:
// each step costs more than the one before (the chips hide sessions already in
// memory; a wider window means re-scanning `projects/`), and a user who finds
// what they wanted after step one never pays for step two. The chips themselves
// are never touched - clearing the query is what puts the view back.
function widenSearch(step) {
    if (step === 'hidden') {
        state.searchIncludeHidden = true;

        // The hidden set includes past sessions, and those are not fetched at all
        // while the history chip is off. Load them first - afterHistoryLoaded
        // starts the search once the complete widened scope is in hand, so the
        // scan runs once rather than once now and again when the history lands.
        if (state.history === null) {
            ensureHistoryLoaded();
            return;
        }
        runSearch();
        return;
    }

    if (step !== 'range') {
        return;
    }

    // Checked before the flag is set: with no wider window to move to there is
    // nothing to widen, and flagging the scope anyway would leave the search
    // reading past the chips with no re-scan and no notice to undo it. The offer
    // is never rendered in that state, but `step` comes from a DOM attribute.
    const next = logic.nextHistoryRange(state.historyRange);
    if (!next) {
        return;
    }
    state.searchIncludeHidden = true;
    // Re-fetches and re-runs the search through afterHistoryLoaded; the chip
    // stays as it is (see setHistoryRange).
    setHistoryRange(next, false);
}

// Give the widening back without giving up the query - the only other way out
// is editing the search text, which destroys the very query that led here.
// Deliberately no re-scan: the extra rows disappear because `widened` is false
// again, and re-reading files that are already answered for would cost a
// progress bar and a full row flicker for what is an undo. The match set is
// pruned to the chip-defined scope instead, so it keeps describing exactly what
// was searched - otherwise the chip counts would go on reporting matches in
// sessions that are no longer being read.
function resetWidenedSearch() {
    state.searchIncludeHidden = false;

    if (state.searchMatches instanceof Map) {
        const inScope = new Set(collectSearchRefs(true).map((ref) => ref.session_id));
        state.searchMatches = new Map([...state.searchMatches].filter(([id]) => inScope.has(id)));
    }
    if (state.last) {
        render(state.last);
    }
}

// One streaming update from the backend: {seq, processed, total, matches, done,
// error}, each match {session_id, count, snippets, partial?}. A stale seq (a
// newer search has started) is ignored. Pure progress ticks only move the bar; a
// re-render happens when new matches arrive or the scan ends.
function onSearchPush(payload) {
    if (!payload || payload.seq !== state.searchSeq) {
        return;
    }

    // The search could not complete: an invalid regular expression, or the
    // backend scan failed. Either way, do not present it as "no matches".
    if (payload.error) {
        state.searchError = true;
        state.searchLoading = false;
        state.searchMatches = null;
        updateSearchProgress();
        updateSearchBox();
        if (state.last) {
            render(state.last);
        }
        return;
    }

    state.searchProcessed = payload.processed || 0;
    state.searchTotal = payload.total || 0;

    let changed = false;
    if (Array.isArray(payload.matches) && payload.matches.length) {
        if (!(state.searchMatches instanceof Map)) {
            state.searchMatches = new Map();
        }
        for (const match of payload.matches) {
            // A match without an id cannot be attached to a row; a session
            // already matched keeps the first result, since a delta rescan only
            // ever re-reads sessions that were not matched before.
            if (!match || !match.session_id || state.searchMatches.has(match.session_id)) {
                continue;
            }
            state.searchMatches.set(match.session_id, match);
            changed = true;
        }
    }
    if (payload.done) {
        state.searchLoading = false;
        changed = true;
    }

    updateSearchProgress();
    if (changed && state.last) {
        render(state.last);
    }
}

// A thin determinate progress bar under the toolbar while a scan runs; before
// the first update arrives (total unknown) it reads as indeterminate.
function updateSearchProgress() {
    const bar = document.getElementById('search-progress');
    if (!bar) {
        return;
    }

    bar.hidden = !state.searchLoading;
    const indeterminate = state.searchLoading && state.searchTotal === 0;
    bar.classList.toggle('indeterminate', indeterminate);

    const fill = bar.querySelector('.search-progress-fill');
    if (fill) {
        const pct = state.searchTotal > 0
            ? Math.min(100, Math.round((state.searchProcessed / state.searchTotal) * 100))
            : 0;
        fill.style.width = indeterminate ? '' : pct + '%';
    }
}

// Preview-only fallback (no bridge, so no transcript access): match the query
// against the mock records' visible fields, over the same in-view scope the real
// search uses. The hits it fabricates have the shape the backend sends, so the
// preview exercises the excerpt rendering too. Never used in the packaged app.
function mockSearchMatches(query) {
    const needle = query.toLowerCase();
    const scope = new Set(currentSessionRefs().map((ref) => ref.session_id));
    const matches = new Map();
    const scan = (list) => {
        for (const raw of list || []) {
            if (!scope.has(raw.session_id)) {
                continue;
            }
            const hay = [raw.title, raw.short_name, raw.cwd, raw.model_id].filter(Boolean).join(' ');
            const at = hay.toLowerCase().indexOf(needle);
            if (at < 0) {
                continue;
            }
            matches.set(raw.session_id, {
                session_id: raw.session_id,
                count: 3,
                snippets: [{
                    kind: 'user',
                    before: hay.slice(Math.max(0, at - 40), at),
                    match: hay.slice(at, at + needle.length),
                    after: hay.slice(at + needle.length, at + needle.length + 40),
                    clipped_before: at > 40,
                    clipped_after: hay.length > at + needle.length + 40,
                }],
            });
        }
    };
    scan(state.last ? state.last.sessions : []);
    scan(visibleHistory());
    return matches;
}
