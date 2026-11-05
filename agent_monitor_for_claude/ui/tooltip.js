'use strict';

/* --- tooltip ---

   The one and only tooltip in the app. Never use the browser's native `title`
   tooltip (ugly, uncontrollable delay/placement, no theming) - give an element a
   `data-tip` attribute (newlines become line breaks) and this shared, themed,
   HTML tooltip shows it on hover. A single delegated listener handles every
   `data-tip` element, so reconciled rows need no per-node wiring. */
const TOOLTIP_DELAY = 350;

let tooltipEl = null;
let tooltipTarget = null;
let tooltipTimer = null;
// Where the pointer was when it last entered a trigger. A render can replace the
// trigger node under a motionless pointer, and this is what lets syncTooltip ask
// "is an equivalent trigger still under the cursor" instead of just giving up.
let tooltipPoint = { x: 0, y: 0 };
// The trigger a click just dismissed the tooltip of: it stays quiet until the
// pointer leaves it, so whatever the click opened is not covered again.
let tooltipSilenced = null;

function ensureTooltip() {
    if (!tooltipEl || !tooltipEl.isConnected) {
        tooltipEl = document.createElement('div');
        tooltipEl.className = 'tooltip';
        tooltipEl.setAttribute('role', 'tooltip');
        // The visible tooltip has pointer-events: auto, so it catches the whole
        // press instead of leaking it to the element behind it; on the click that
        // completes on the tooltip we just dismiss, acting on nothing underneath.
        tooltipEl.addEventListener('click', hideTooltip);
        tooltipEl.addEventListener('pointerleave', hideTooltip);
        document.body.appendChild(tooltipEl);
    }
    return tooltipEl;
}

function positionTooltip(target) {
    const tip = tooltipEl;
    const rect = target.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;

    let top = rect.bottom + gap;
    if (top + height + margin > window.innerHeight) {
        top = rect.top - height - gap;
    }
    let left = Math.min(rect.left, window.innerWidth - width - margin);
    left = Math.max(margin, left);
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(Math.max(margin, top)) + 'px';
}

function hideTooltip() {
    clearTimeout(tooltipTimer);
    tooltipTimer = null;
    tooltipTarget = null;
    if (tooltipEl) {
        tooltipEl.classList.remove('show');
    }
}

function initTooltips() {
    ensureTooltip();
    document.addEventListener('pointerover', (event) => {
        // Recorded before the early return below, so it keeps up as the pointer
        // moves across a trigger's own children.
        tooltipPoint = { x: event.clientX, y: event.clientY };

        const target = event.target.closest ? event.target.closest('[data-tip]') : null;
        if (!target || target === tooltipTarget || target === tooltipSilenced) {
            return;
        }
        clearTimeout(tooltipTimer);
        tooltipTarget = target;
        tooltipTimer = setTimeout(() => {
            if (tooltipTarget !== target || !target.isConnected || !target.dataset.tip) {
                return;
            }
            const tip = ensureTooltip();
            tip.textContent = target.dataset.tip;   // .tooltip has white-space: pre-line
            positionTooltip(target);
            tip.classList.add('show');
        }, TOOLTIP_DELAY);
    });
    document.addEventListener('pointerout', (event) => {
        const target = event.target.closest ? event.target.closest('[data-tip]') : null;
        if (target && target === tooltipSilenced && !(event.relatedTarget && target.contains(event.relatedTarget))) {
            tooltipSilenced = null;
        }
        if (!target || target !== tooltipTarget) {
            return;
        }
        // Moving onto the target's own children, or onto the tooltip itself,
        // must not dismiss it - the tooltip stays reachable so it can be clicked.
        const into = event.relatedTarget;
        if (into && (target.contains(into) || (tooltipEl && tooltipEl.contains(into)))) {
            return;
        }
        hideTooltip();
    });
    // A press acts on the element under the tooltip's target - it expands a
    // section, opens a menu, selects a row - and the tooltip would cover what
    // just appeared. Drop it, and keep its target quiet until the pointer leaves.
    document.addEventListener('pointerdown', () => {
        tooltipSilenced = tooltipTarget;
        hideTooltip();
    }, true);
    // A moved/scrolled target leaves a stale tooltip behind - drop it.
    document.addEventListener('scroll', hideTooltip, true);
    window.addEventListener('resize', hideTooltip);
}

// The tooltip is dismissed by the pointer leaving its target - so a target that
// leaves the DOM instead dismisses nothing, and the popup stands over content it
// no longer describes. Two ways that happens, both routine: a render drops the
// target outright (the widened-search notice on reset), or replaces it with an
// equivalent node (a slot re-rendered from markup, after which the later
// pointerout arrives on the NEW node, fails the `!== tooltipTarget` check, and
// never fires at all - so such a tooltip would stick indefinitely).
// Reconciling the popup against a render is what syncOpenMenu already does for
// the menu; this is the same for the tooltip.
function syncTooltip() {
    if (!tooltipTarget || tooltipTarget.isConnected) {
        return;
    }

    const under = document.elementFromPoint(tooltipPoint.x, tooltipPoint.y);
    // The pointer is on the tooltip itself (it is clickable, so it can be
    // hovered): the user is reading it, and its own leave handler owns it.
    if (under && tooltipEl && tooltipEl.contains(under)) {
        return;
    }

    // Replaced rather than gone: re-anchor instead of blinking the tooltip out
    // from under someone who is still pointing at the very same control.
    const replacement = under && under.closest ? under.closest('[data-tip]') : null;
    if (replacement && replacement.dataset.tip === tooltipTarget.dataset.tip) {
        tooltipTarget = replacement;
        if (tooltipEl && tooltipEl.classList.contains('show')) {
            positionTooltip(replacement);
        }
        return;
    }

    hideTooltip();
}
