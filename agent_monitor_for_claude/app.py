"""
Application
===========

Hosts the pywebview window and exposes a small JavaScript bridge.  The UI
polls ``get_snapshot`` on an interval and renders the result; ``get_bootstrap``
supplies static configuration (labels, theme, poll interval) once on load.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import winreg
from pathlib import Path
from typing import Any

import webview  # type: ignore[import-untyped]  # no type stubs available

from . import __version__
from .clipboard import copy_text as _copy_text
from .history import list_history
from .i18n import T
from .paths import config_dir
from .pricing import load_pricing
from .search import run_search
from .session_delete import delete_session as _delete_session
from .settings import POLL_INTERVAL, WINDOW_HEIGHT, WINDOW_WIDTH
from .snapshot import build_snapshot, registry_fingerprint
from .verbose import print_runtime_diagnostics
from .window_focus import focus_session_window, open_directory, open_vscode_session

__all__ = ['run']

# Initial window paint colors matching the UI themes, so the window does not
# flash in the wrong brightness before the page loads.  The page itself picks
# the stored theme (or the system preference) before first paint.
_WINDOW_BACKGROUND_DARK = '#0d0f14'
_WINDOW_BACKGROUND_LIGHT = '#eef1f6'


def _window_background() -> str:
    """Match the initial window color to the Windows app theme."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            apps_use_light, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
    except OSError:
        return _WINDOW_BACKGROUND_DARK

    return _WINDOW_BACKGROUND_LIGHT if apps_use_light else _WINDOW_BACKGROUND_DARK


# Cap on a forwarded UI log line so a runaway message cannot flood the console.
_LOG_MAX_LEN = 2000


def _sanitize_log(message: object) -> str:
    """Render an untrusted UI log message safe to print.

    Escapes control characters (except tab/newline), C1 controls, DEL, and lone
    surrogates - so crafted page text cannot retitle the window, clear the
    screen, or overwrite earlier output via ANSI/OSC sequences, nor raise on a
    strict-encoded stream - and caps the length.
    """
    out = []
    for ch in str(message):
        code = ord(ch)
        printable = ch in '\t\n' or (0x20 <= code != 0x7f and not (0x80 <= code <= 0x9f) and not (0xd800 <= code <= 0xdfff))
        out.append(ch if printable else '\\x{:02x}'.format(code))

    cleaned = ''.join(out)
    if len(cleaned) > _LOG_MAX_LEN:
        cleaned = cleaned[:_LOG_MAX_LEN] + '...'
    return cleaned


class _MonitorApi:
    """Methods exposed to JavaScript via pywebview's JS bridge."""

    def __init__(self) -> None:
        # The window is attached once created (run below), so search results can
        # be pushed into the page as they are found. `_search_seq` is the id of
        # the latest search the UI started: a running search whose id no longer
        # matches has been superseded and cancels itself.
        self._window: Any = None
        self._search_seq = 0
        self._search_lock = threading.Lock()

    def attach_window(self, window: Any) -> None:
        """Bind the pywebview window used to push streaming search results."""
        self._window = window

    def log(self, message: object) -> None:
        """Forward a UI-side diagnostic message to stderr.

        The window runs headless (no attached console), and JavaScript errors
        stay inside WebView2 where they are invisible from the terminal.  The
        UI's global error handler calls this so failures surface on stderr
        (visible when the app is launched with ``--verbose``).  In a windowed
        build without ``--verbose`` there is no stderr, so this is a no-op.
        """
        if sys.stderr is None:
            return

        print('[UI]', _sanitize_log(message), file=sys.stderr, flush=True)

    def get_snapshot(self) -> dict[str, Any]:
        """Return the current session overview grouped by project."""
        return build_snapshot()

    def get_fingerprint(self) -> str:
        """Return the cheap registry/transcript change fingerprint."""
        return registry_fingerprint()

    def get_history(self) -> list[dict[str, Any]]:
        """Return past, non-live sessions for the history listing (on demand).

        The UI calls this only while its history filter is enabled, so the
        ``projects/`` scan never runs on the per-second poll.  pywebview
        dispatches it on a worker thread, so the potentially second-long scan
        does not block the WebView; the JS side awaits the returned promise and
        shows a loading state meanwhile.
        """
        return list_history()

    def start_search(self, query: object, sessions: object, options: object, seq: object) -> bool:
        """Start a streaming content search over the given in-view sessions.

        Content-based but strictly encapsulated (see ``search``): the scan reads
        transcripts locally and pushes back only matching session ids and
        progress counts - never any conversation text.  ``options`` carries the
        editor toggles (match case, whole word, regular expression).  Returns
        immediately; the scan runs on its own daemon thread and reports through
        ``window.__amcSearchPush`` as it goes, so results and the progress bar
        fill in live.  ``seq`` is the UI's monotonic search id: starting a new
        search bumps the active id, which makes any still-running earlier search
        cancel itself.
        """
        try:
            seq_value = int(seq)  # type: ignore[arg-type]  # validated below
        except (TypeError, ValueError):
            return False

        # pywebview dispatches each js_api call on its own worker thread with no
        # ordering guarantee, so a later start_search can arrive before an earlier
        # one. Never regress to an older seq: a stale, lower seq would abort the
        # active search and get its pushes dropped, stranding the UI in
        # "Searching...". A page reload resets the UI counter, and get_bootstrap
        # resets this one to match, so the guard never rejects a fresh page.
        with self._search_lock:
            self._search_seq = max(self._search_seq, seq_value)

        thread = threading.Thread(target=self._run_search, args=(query, sessions, options, seq_value), daemon=True)
        thread.start()
        return True

    def _run_search(self, query: object, sessions: object, options: object, seq: int) -> None:
        """Worker body: scan transcripts and push updates until done or superseded."""
        def cancelled() -> bool:
            return self._search_seq != seq

        def on_update(processed: int, total: int, matches: list[str], done: bool, error: bool) -> None:
            self._push_search(seq, processed, total, matches, done, error)

        try:
            run_search(query, sessions, options, on_update, cancelled)
        except Exception:
            # A search thread must never crash the app; tell the UI it finished
            # (so it clears its loading/progress state) but as an error, not a
            # clean empty result - otherwise a failed scan reads as a confident
            # "no session contains this text".
            if not cancelled():
                self._push_search(seq, 0, 0, [], True, True)

    def _push_search(self, seq: int, processed: int, total: int, matches: list[str], done: bool, error: bool) -> None:
        """Push one search update into the page via the window bridge.

        A superseded search (its id no longer current) is dropped, so a stale
        scan cannot write results over a newer one.  Only ids, counts, and an
        error flag cross the bridge - never conversation content.
        """
        window = self._window
        if window is None or self._search_seq != seq:
            return

        payload = json.dumps({
            'seq': seq, 'processed': processed, 'total': total,
            'ids': list(matches), 'done': done, 'error': error,
        })
        try:
            window.evaluate_js('window.__amcSearchPush && window.__amcSearchPush(' + payload + ')')
        except Exception:
            # The window may be closing, or the bridge briefly unavailable - a
            # dropped progress update is harmless.
            pass

    def delete_session(self, session_id: object, cwd: object) -> bool:
        """Delete a past session's transcript and subagent folder (user-initiated).

        Only ever invoked from the history listing's per-row action, after an
        in-UI confirmation.  All safety guards live in ``session_delete``: a UUID
        check, a refusal for any session with a live process, and path
        confinement to ``projects/``.
        """
        if not isinstance(session_id, str) or not isinstance(cwd, str):
            return False

        return _delete_session(session_id, cwd)

    def copy_text(self, text: object) -> bool:
        """Copy the given text to the clipboard (user-initiated)."""
        if not isinstance(text, str) or not text:
            return False

        return _copy_text(text)

    def open_path(self, path: object) -> bool:
        """Open a session's project directory in Windows Explorer (user-initiated)."""
        if not isinstance(path, str) or not path:
            return False

        return open_directory(path)

    def focus_session(self, pid: object, project_name: object = '', session_id: object = '', vscode_deeplink: object = False,
                      session_title: object = '') -> bool:
        """Jump to a session: raise its hosting window, then focus its tab if possible.

        For sessions of the VS Code extension the official deep link
        (``vscode://anthropic.claude-code/open?session=...``) focuses the exact
        session tab.  The right window is raised first, because VS Code routes
        the deep link to the currently focused window.  For a session running in
        an external terminal, *session_title* lets its terminal window be found
        when no window sits on the process chain.
        """
        if isinstance(pid, bool) or not isinstance(pid, (int, float, str)):
            return False

        try:
            pid_value = int(pid)
        except (TypeError, ValueError, OverflowError):
            # OverflowError covers a non-finite float (int(float('inf'))); NaN is
            # a ValueError. Either way, degrade to a graceful refusal.
            return False

        name = project_name if isinstance(project_name, str) else ''
        title = session_title if isinstance(session_title, str) else ''
        focused = focus_session_window(pid_value, name, title)

        if vscode_deeplink is True and isinstance(session_id, str) and session_id:
            if focused:
                time.sleep(0.3)
            return open_vscode_session(session_id) or focused

        return focused

    def get_bootstrap(self) -> dict[str, Any]:
        """Return static UI configuration loaded once when the page starts."""
        # A fresh page restarts the UI's search-seq counter at 0, so reset the
        # backend's to match - otherwise the monotonic guard in start_search
        # would keep rejecting the reloaded page's new, lower seqs.
        with self._search_lock:
            self._search_seq = 0

        return {
            'labels': dict(T),
            'poll_interval': POLL_INTERVAL,
            'version': __version__,
            'default_effort': _default_effort(),
            'pricing': load_pricing(),
        }


def run(verbose: bool = False) -> None:
    """Create the window and start the pywebview event loop (blocking).

    The WebView2 profile is persistent (``private_mode=False``) so that UI
    preferences kept in localStorage - theme, filter, collapsed panels -
    survive restarts.  pywebview's default private mode would reset them on
    every launch.

    Parameters
    ----------
    verbose
        When true, print post-init runtime diagnostics (webview renderer, GUI
        backend) once the event loop is running.  These are only available
        after the CLR/WebView2 has loaded, so they run as the post-start hook.
    """
    api = _MonitorApi()

    ui_dir = _ui_dir()
    window = webview.create_window(
        # .get with an English default so an empty T (all locales failed to load)
        # degrades to a titled window instead of crashing startup with a KeyError.
        T.get('app_title', 'Agent Monitor for Claude'),
        url=f'{ui_dir / "index.html"}?v={_asset_version(ui_dir)}',
        js_api=api,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(480, 360),
        background_color=_window_background(),
    )
    # Let the bridge push streaming search results back into this window.
    api.attach_window(window)
    on_started = print_runtime_diagnostics if verbose else None
    webview.start(on_started, private_mode=False, storage_path=str(_storage_dir()), icon=_icon_path())


def _default_effort() -> str:
    """Read the global default effort level from Claude Code's settings file.

    A per-session effort override is not persisted anywhere on disk, so only
    this default can be surfaced.
    """
    try:
        data = json.loads((config_dir() / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return ''

    value = data.get('effortLevel') if isinstance(data, dict) else None
    return value if isinstance(value, str) else ''


def _icon_path() -> str | None:
    """Return the window icon file, or ``None`` to use the default.

    A frozen build carries the icon inside the executable, so pywebview's
    fallback (extracting it from ``sys.executable``) already shows the right
    one.  When running from source, ``sys.executable`` is ``python.exe`` and
    that fallback yields the Python icon, so the bundled ``.ico`` at the
    project root is handed to pywebview explicitly.
    """
    if getattr(sys, 'frozen', False):
        return None

    candidate = Path(__file__).parent.parent / 'agent_monitor_for_claude.ico'
    return str(candidate) if candidate.is_file() else None


def _storage_dir() -> Path:
    """Return the WebView2 profile directory used for UI preference storage."""
    base = os.environ.get('LOCALAPPDATA')
    root = Path(base) if base else Path.home() / 'AppData' / 'Local'
    return root / 'AgentMonitorForClaude'


def _asset_version(ui_dir: Path) -> str:
    """Return a short content fingerprint of the bundled UI assets.

    WebView2 serves the UI over a fixed-port localhost origin with a persistent
    profile, so its HTTP cache can outlive an app update and keep serving an
    old asset.  Appending this token as a ``?v=`` query gives a changed asset a
    fresh URL - and thus a fresh cache key - while leaving the origin untouched,
    so the localStorage UI preferences (keyed by origin, not query) survive.

    Parameters
    ----------
    ui_dir
        Directory holding the served UI asset files.

    Returns
    -------
    str
        A 12-character hex token that changes when any asset's content changes.
    """
    digest = hashlib.sha256()
    try:
        names = sorted(os.listdir(ui_dir))
    except OSError:
        return '0'

    for name in names:
        path = ui_dir / name
        if not path.is_file():
            continue
        try:
            digest.update(name.encode('utf-8'))
            digest.update(path.read_bytes())
        except OSError:
            continue

    return digest.hexdigest()[:12]


def _ui_dir() -> Path:
    """Return the directory holding the bundled UI assets (source or frozen)."""
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS')) / 'agent_monitor_for_claude' / 'ui'

    return Path(__file__).parent / 'ui'
