"""
Session Roots
=============

Composes the full list of places a Claude Code session can live: the native
Windows install (:func:`paths.windows_root`) plus one entry per running WSL
distro with a ``.claude`` directory (:func:`wsl.wsl_roots`).  Every other
module that needs to enumerate every root, or resolve one the UI already
named, goes through this module rather than combining ``paths`` and ``wsl``
itself.

Resolving a UI-supplied origin back to its root is a deliberate refusal, never
a fallback: an origin that does not exactly match a currently discovered root
- most commonly a WSL distro that has since stopped running - returns
``None`` rather than silently substituting another root.  A stale UI call
must not touch another root; a distro gone from the running list becomes
unreachable by design.
"""
from __future__ import annotations

from .paths import SessionRoot, windows_root
from .wsl import wsl_roots

__all__ = ['root_for_origin', 'session_roots']


def session_roots() -> list[SessionRoot]:
    """Return every currently available session root.

    Returns
    -------
    list[SessionRoot]
        The native Windows root, always first, followed by one entry per
        running WSL distro with a ``.claude`` directory (see
        :func:`wsl.wsl_roots` for the discovery gates and ordering).
    """
    return [windows_root(), *wsl_roots()]


def root_for_origin(origin: object) -> SessionRoot | None:
    """Return the currently discovered root whose ``origin`` exactly matches *origin*.

    This is always a refusal on no match, never a fallback to another root -
    see the module docstring.  A non-``str`` *origin* is rejected outright,
    since it can only be a stale or malformed value, never a real
    ``SessionRoot.origin``.

    Parameters
    ----------
    origin : object
        A value as supplied by the caller (typically a UI-held ``origin``
        field from an earlier snapshot); untrusted, hence typed as ``object``.

    Returns
    -------
    SessionRoot or None
        The matching root, or ``None`` when *origin* is not a string or
        matches no root currently returned by :func:`session_roots`.
    """
    if not isinstance(origin, str):
        return None

    for root in session_roots():
        if root.origin == origin:
            return root

    return None
