'use strict';

/* --- fancy menu (filter and sort dropdowns + per-row meatball) --- */

// A single floating menu is reused for the filter and sort dropdowns and every
// row's meatball menu; only one can be open at a time. The state remembers what
// the menu is bound to (a dropdown trigger, or a specific session's row button)
// so a re-render can re-anchor it instead of tearing it down.
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
    closeProcPanel();
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

// Mount a built menu under its anchor and wire the dismissal every menu shares:
// an outside click or Escape closes it, scrolling and resizing follow the anchor.
// A click inside the menu leaves it open - the item menus close themselves on
// select, the filter list stays open while several boxes are ticked.
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
    };

    menu.onDocClick = (event) => {
        if (!el.contains(event.target) && !menu.anchorEl.contains(event.target) && event.target !== menu.anchorEl) {
            closeMenu();
        }
    };
    menu.onKey = (event) => {
        if (event.key === 'Escape') {
            closeMenu();
        }
    };
    // Scroll / resize move the anchor, so follow it. Crucially this never
    // closes the menu - a data refresh (which does not scroll) must leave it
    // open; re-anchoring after a render is handled by syncOpenMenu.
    menu.onViewportChange = () => {
        if (menu.anchorEl && menu.anchorEl.isConnected) {
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

    positionMenu(el, anchor, menu.align);
}

// Called after each render. The DOM node a menu was anchored to may have moved
// (rows reorder) but is reused, so re-bind to the current node and reposition.
// A row menu whose session has disappeared is the only case that closes a menu
// automatically.
function syncOpenMenu() {
    const menu = openMenuState;
    if (!menu) {
        return;
    }

    let anchor = null;
    if (menu.type === 'sort') {
        anchor = document.getElementById('sort-trigger');
    } else if (menu.type === 'filters') {
        anchor = document.getElementById('filter-trigger');
    } else if (menu.type === 'row') {
        anchor = findRowMenuButton(menu.rowKey);
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
    positionMenu(menu.el, anchor, menu.align);
}

function findRowMenuButton(rowKey) {
    if (!rowKey) {
        return null;
    }
    const rows = document.querySelectorAll('.row');
    for (const row of rows) {
        if (row.dataset.key === rowKey) {
            return row.querySelector('.row-menu-btn');
        }
    }
    return null;
}

function positionMenu(menu, anchor, align) {
    const rect = anchor.getBoundingClientRect();
    const margin = 6;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;

    // Right-align the menu under the anchor, then clamp to the viewport. A
    // left-aligned menu (the filter list) keeps its left edge on the anchor's,
    // so a trigger whose text changes width never shifts the open list.
    let left;
    if (align === 'left') {
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
