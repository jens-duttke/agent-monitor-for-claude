'use strict';

/* --- the floating menu: the session menu (right-click on a list entry) and
   the copy menu (right-click on selected text) --- */

// One floating menu at a time. The state remembers which session it belongs to,
// so a re-render can follow the entry instead of tearing the menu down.
let openMenuState = null;

function closeMenu() {
    const menu = openMenuState;
    if (!menu) {
        return;
    }
    openMenuState = null;
    document.removeEventListener('click', menu.onDocClick, true);
    document.removeEventListener('keydown', menu.onKey, true);
    document.removeEventListener('scroll', menu.onViewportChange, true);
    window.removeEventListener('resize', menu.onViewportChange);
    if (menu.anchorEl) {
        menu.anchorEl.classList.remove('menu-open');
    }
    menu.el.remove();
}

// Make room for a menu on `anchor`. Clicking the same trigger again toggles the
// menu shut, so this returns false when the click was the one closing it.
function claimMenuSlot(anchor) {
    const toggleShut = openMenuState && openMenuState.anchorEl === anchor;
    closeMenu();
    return !toggleShut;
}

function openMenu(anchor, items, onSelect, context) {
    if (!claimMenuSlot(anchor)) {
        return;
    }

    const el = document.createElement('div');
    el.className = 'menu';
    el.setAttribute('role', 'menu');

    items.forEach((item) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'menu-item' + (item.active ? ' active' : '') + (item.danger ? ' danger' : '');
        button.setAttribute('role', 'menuitem');
        button.innerHTML = '<span class="menu-check" aria-hidden="true">&#10003;</span>'
            + '<span class="menu-label"></span>';
        button.querySelector('.menu-label').textContent = item.label;
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            closeMenu();
            onSelect(item.key);
        });
        el.appendChild(button);
    });

    showMenu(el, anchor, context);
}

// Mount a built menu at its anchor (or at the pointer, for a right-click) and
// wire the dismissal: an outside click or Escape closes it, scrolling and
// resizing follow the anchor. Its items close it themselves on select.
function showMenu(el, anchor, context) {
    document.body.appendChild(el);
    anchor.classList.add('menu-open');

    const menu = {
        el,
        anchorEl: anchor,
        type: context.type,
        // The full row key (session_id + pid), not the session id alone: two rows
        // can share a session id (a live window plus a --resume terminal, or a
        // live+history duplicate), and re-anchoring on the id would jump to the
        // wrong one after rows reorder.
        rowKey: context.rowKey || null,
        align: context.align || 'right',
        // A right-click opens the menu where the pointer is.
        point: context.point || null,
    };

    menu.onDocClick = (event) => {
        // A copy menu's anchor is only the element right-clicked, which a click
        // into it does not toggle - so any click outside the menu closes it.
        const onAnchor = menu.type !== 'selection' && (menu.anchorEl.contains(event.target) || event.target === menu.anchorEl);
        if (!el.contains(event.target) && !onAnchor) {
            closeMenu();
        }
    };
    menu.onKey = (event) => {
        if (event.key === 'Escape') {
            // The Escape is spent on the menu: without this the page's own
            // handler would also leave the detail.
            event.stopPropagation();
            closeMenu();
        }
    };
    // Scroll / resize move the anchor, so follow it. Crucially this never
    // closes the menu - a data refresh (which does not scroll) must leave it
    // open; re-anchoring after a render is handled by syncOpenMenu.
    menu.onViewportChange = () => {
        // A menu at the pointer stays where it opened: the detail's consoles
        // scroll themselves every second, and that must not close it.
        if (!menu.point && menu.anchorEl && menu.anchorEl.isConnected) {
            positionMenu(el, menu.anchorEl, menu.align);
        }
    };

    openMenuState = menu;

    // Defer the outside-click listener so the click that opened the menu does
    // not immediately close it.
    setTimeout(() => {
        if (openMenuState === menu) {
            document.addEventListener('click', menu.onDocClick, true);
        }
    }, 0);
    document.addEventListener('keydown', menu.onKey, true);
    document.addEventListener('scroll', menu.onViewportChange, true);
    window.addEventListener('resize', menu.onViewportChange);

    positionMenu(el, anchor, menu.align, menu.point);
    const first = el.querySelector('.menu-item');
    if (first && context.focusFirst) {
        first.focus();
    }
}

// Called after each render. The DOM node a menu was anchored to may have moved
// (rows reorder) but is reused, so re-bind to the current node and reposition.
// A menu whose session has disappeared closes.
function syncOpenMenu() {
    const menu = openMenuState;
    if (!menu) {
        return;
    }

    // A copy menu holds its text already and sits at the pointer, so a render
    // that replaced the selected element leaves it open where it is.
    if (menu.type === 'selection') {
        return;
    }

    let anchor = null;
    if (menu.type === 'row') {
        anchor = findItem(menu.rowKey);
    }

    if (!anchor) {
        closeMenu();
        return;
    }

    if (anchor !== menu.anchorEl) {
        menu.anchorEl.classList.remove('menu-open');
        anchor.classList.add('menu-open');
        menu.anchorEl = anchor;
    }
    if (!menu.point) {
        positionMenu(menu.el, anchor, menu.align);
    }
}

function positionMenu(menu, anchor, align, point) {
    const rect = point
        ? { left: point.x, right: point.x, top: point.y, bottom: point.y }
        : anchor.getBoundingClientRect();
    const margin = 6;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;

    // Right-align the menu under the anchor (left-align it at a pointer), then
    // clamp to the viewport.
    let left;
    if (align === 'left' || point) {
        left = Math.min(rect.left, window.innerWidth - width - margin);
    } else {
        left = rect.right - width;
        if (left < margin) {
            left = Math.min(rect.left, window.innerWidth - width - margin);
        }
    }
    left = Math.max(margin, left);

    let top = rect.bottom + 4;
    if (top + height > window.innerHeight - margin) {
        const above = rect.top - height - 4;
        top = above >= margin ? above : Math.max(margin, window.innerHeight - height - margin);
    }

    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
}
