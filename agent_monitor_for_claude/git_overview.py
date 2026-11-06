"""
Git overview
============

Where the work in each listed session's directory stands, for the list itself:
how many files are not committed, how many commits are not pushed, whether the
branch is on its remote at all, and whether its commits are already in the
remote's default branch.

What the remote holds is read from the repository's own remote-tracking refs,
so it is the state of the last ``git fetch`` - this module never contacts a
remote - and the time of that fetch is passed on with it.

The list must never wait for git: a directory over ``\\\\wsl.localhost`` takes
seconds per call.  :func:`git_overview` therefore answers at once from a cache
and hands every directory without a fresh answer to a small pool of worker
threads; an answer that arrives later is picked up by the next request.  An
outdated answer keeps being shown while its refresh runs.  Every git call goes
through :mod:`agent_monitor_for_claude.git_status`'s runner - fixed arguments,
an existing directory, a timeout, no window, no index write.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .git_status import _git as run_git

__all__ = ['PENDING', 'git_overview', 'remote_web_address']

# The answer for a directory git has not reported on yet.
PENDING = 'pending'
# How long an answer counts as current; an older one is refreshed in the background and shown meanwhile.
_FRESH_SECONDS = 30.0
# A directory nobody asked about for this long is forgotten.
_FORGET_SECONDS = 600.0
# Directories read at once; each runs up to four short git calls.
_WORKERS = 6
# The most directories one request may name.
_MAX_DIRECTORIES = 200
# The names a remote's default branch is looked for under when its HEAD is not set.
_DEFAULT_BRANCH_NAMES = ('main', 'master')
# A branch that changed more files than this is not checked for a squash merge (its paths would not fit one command line).
_MAX_COMPARED_FILES = 300
# A remote written the scp way: ``git@host:owner/repo.git``.
_SCP_REMOTE = re.compile(r'^[A-Za-z0-9._-]+@([A-Za-z0-9.-]+):(?!//)(.+)$')
# A remote written as a URL: its scheme, an optional user (and token) before an @, the host, an optional port, the path.
_URL_REMOTE = re.compile(r'^(?:https?|ssh|git|git\+ssh)://(?:[^@/]*@)?([A-Za-z0-9.-]+)(?::\d+)?(/[^?#]*)$')
_WEB_HOST = re.compile(r'^[A-Za-z0-9.-]+$')
_WEB_PATH_SEGMENT = re.compile(r'^[A-Za-z0-9._~+-]+$')


@dataclass
class _Entry:
    """One directory's latest answer: when it was read, the record (None: no repository), whether a read is running."""

    read_at: float | None = None
    record: dict[str, Any] | None = None
    running: bool = False
    asked_at: float = 0.0


_entries: dict[str, _Entry] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix='git-overview')


def git_overview(directories: list[str]) -> list[dict[str, Any] | str | None]:
    """Return the latest known state of each directory, starting a read for any that is missing or outdated.

    Parameters
    ----------
    directories : list[str]
        Directories this host can open (a WSL path already translated); only
        the first ``_MAX_DIRECTORIES`` are considered.

    Returns
    -------
    list
        One answer per directory, in order: the record, None when it is no
        directory or no repository, or ``PENDING`` while the first read runs.
    """
    now = time.monotonic()
    answers: list[dict[str, Any] | str | None] = []
    with _lock:
        for directory in directories[:_MAX_DIRECTORIES]:
            key = os.path.normcase(os.path.normpath(directory))
            entry = _entries.setdefault(key, _Entry())
            entry.asked_at = now
            outdated = entry.read_at is None or now - entry.read_at >= _FRESH_SECONDS
            if outdated and not entry.running:
                entry.running = True
                _pool.submit(_refresh, key, directory)
            answers.append(PENDING if entry.read_at is None else entry.record)
        for stale in [key for key, entry in _entries.items() if now - entry.asked_at > _FORGET_SECONDS and not entry.running]:
            del _entries[stale]

    return answers


def _refresh(key: str, directory: str) -> None:
    """Read *directory* and store the answer; a failure stores None rather than leaving the entry running."""
    record = None
    try:
        record = read_directory(directory)
    finally:
        with _lock:
            entry = _entries.setdefault(key, _Entry())
            entry.record = record
            entry.read_at = time.monotonic()
            entry.running = False


def read_directory(directory: str) -> dict[str, Any] | None:
    """Describe where the work in *directory* stands, or None when it is no directory or lies in no repository.

    Returns
    -------
    dict or None
        ``{'branch', 'detached', 'upstream', 'upstream_gone', 'ahead', 'behind',
        'on_remote', 'default_branch', 'unmerged', 'changes_in_default', 'changed',
        'untracked', 'fetched_at', 'remote_web'}`` - ``ahead`` counts the commits
        the remote branch lacks, ``unmerged`` those the remote's default branch
        lacks (None when either is unknown), ``changes_in_default`` whether a
        branch gone from the remote reached it squashed, ``fetched_at`` is the
        epoch second of the last fetch, ``remote_web`` the remote's web address
        (see :func:`remote_web_address`).
    """
    if not isinstance(directory, str) or not directory or not os.path.isdir(directory):
        return None
    status = run_git(directory, 'status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal')
    if status is None:
        return None
    parsed = parse_status_v2(status)
    branch = parsed['branch']
    remote = parsed['upstream'].split('/', 1)[0] if parsed['upstream'] else 'origin'

    refs = _remote_refs(directory, remote, branch, parsed['upstream'])
    default_ref = refs['default']
    remote_branch = refs['branch']

    ahead: int | None = parsed['ahead']
    if parsed['upstream'] and parsed['upstream_gone']:
        ahead = None
    elif not parsed['upstream']:
        # Without an upstream git reports no distance; count against the remote's branch of that name when there is one.
        ahead = _count(directory, remote_branch + '..HEAD') if remote_branch else None

    unmerged = None
    default_branch = default_ref.removeprefix(f'refs/remotes/{remote}/') if default_ref else ''
    if default_ref and not parsed['detached'] and branch and branch != default_branch:
        unmerged = _count(directory, default_ref + '..HEAD')

    # Commits the default branch lacks can still have reached it as one squashed commit; that only
    # matters for a branch no longer on the remote, where it tells "merged" from "deleted".
    in_default = None
    if unmerged and not remote_branch:
        in_default = _changes_in(directory, default_ref)

    return {
        'branch': branch, 'detached': parsed['detached'], 'upstream': parsed['upstream'], 'upstream_gone': parsed['upstream_gone'],
        'ahead': ahead, 'behind': parsed['behind'], 'on_remote': bool(remote_branch),
        'default_branch': default_branch, 'unmerged': unmerged, 'changes_in_default': in_default,
        'changed': parsed['changed'], 'untracked': parsed['untracked'],
        'fetched_at': _fetched_at(directory),
        'remote_web': remote_web_address(run_git(directory, 'remote', 'get-url', '--', remote) or ''),
    }


def parse_status_v2(output: str) -> dict[str, Any]:
    """Read ``status --porcelain=v2 --branch -z`` output into the branch, its upstream and distance, and the file counts.

    An upstream that is configured but has no ``branch.ab`` line no longer
    exists on the remote (as of the last fetch): ``upstream_gone``.
    """
    branch = upstream = ''
    detached = False
    ahead: int | None = None
    behind: int | None = None
    changed = untracked = 0
    records = output.split('\0')
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if record.startswith('# branch.head '):
            head = record[len('# branch.head '):]
            detached = head == '(detached)'
            branch = '' if detached else head
        elif record.startswith('# branch.upstream '):
            upstream = record[len('# branch.upstream '):]
        elif record.startswith('# branch.ab '):
            parts = record[len('# branch.ab '):].split()
            if len(parts) == 2 and parts[0][1:].isdigit() and parts[1][1:].isdigit():
                ahead, behind = int(parts[0][1:]), int(parts[1][1:])
        elif record.startswith(('1 ', 'u ')):
            changed += 1
        elif record.startswith('2 '):
            changed += 1
            # A rename or copy is followed by the path it came from.
            index += 1
        elif record.startswith('? '):
            untracked += 1

    return {
        'branch': branch, 'detached': detached, 'upstream': upstream, 'upstream_gone': bool(upstream) and ahead is None,
        'ahead': ahead or 0, 'behind': behind or 0, 'changed': changed, 'untracked': untracked,
    }


def _remote_refs(directory: str, remote: str, branch: str, upstream: str) -> dict[str, str]:
    """The remote's default branch ref and the remote branch for *branch*, each '' when the remote-tracking refs lack it."""
    patterns = [f'refs/remotes/{remote}/HEAD'] + [f'refs/remotes/{remote}/{name}' for name in _DEFAULT_BRANCH_NAMES]
    wanted = f'refs/remotes/{upstream}' if upstream else (f'refs/remotes/{remote}/{branch}' if branch else '')
    if wanted:
        patterns.append(wanted)
    output = run_git(directory, 'for-each-ref', '--format=%(refname)%09%(symref)', *patterns) or ''

    found: dict[str, str] = {}
    for line in output.splitlines():
        name, _, target = line.partition('\t')
        found[name] = target

    default = found.get(f'refs/remotes/{remote}/HEAD', '')
    if not default:
        default = next((f'refs/remotes/{remote}/{name}' for name in _DEFAULT_BRANCH_NAMES if f'refs/remotes/{remote}/{name}' in found), '')

    return {'default': default, 'branch': wanted if wanted and wanted in found else ''}


def _changes_in(directory: str, default_ref: str) -> bool | None:
    """Whether every file the branch changed since it left *default_ref* reads the same there now, or None when unknown.

    That is what a squash merge leaves behind: the branch's commits are not in
    the default branch, but its result is.  A file the default branch changed
    again since then reads as not merged - the cautious answer.
    """
    names = run_git(directory, 'diff', '--name-only', '-z', '--no-renames', default_ref + '...HEAD', '--')
    if names is None:
        return None
    paths = [path for path in names.split('\0') if path]
    if not paths or len(paths) > _MAX_COMPARED_FILES:
        return None
    differing = run_git(directory, '--literal-pathspecs', 'diff', '--name-only', '--no-renames', 'HEAD', default_ref, '--', *paths)
    if differing is None:
        return None

    return not differing.strip()


def _count(directory: str, revision_range: str) -> int | None:
    """The number of commits in *revision_range*, or None when git cannot say."""
    output = run_git(directory, 'rev-list', '--count', revision_range, '--')
    if output is None or not output.strip().isdigit():
        return None

    return int(output.strip())


def remote_web_address(url: str) -> str:
    """The web address of the repository a remote URL names - the host and the repository's path, over TLS - or '' when it is none.

    ``git@host:owner/repo.git``, ``ssh://git@host:2222/owner/repo.git`` and a
    web URL with ``user:token@`` in it all read as the same address: the user,
    a password or token, an ssh port, the ``.git`` ending and any query are
    dropped here, so none of them ever leaves this module.  A local path or a
    host or path with unusual characters gives ''.
    """
    text = url.strip() if isinstance(url, str) else ''
    match = _SCP_REMOTE.match(text) or _URL_REMOTE.match(text)
    if not match:
        return ''
    host, path = match.group(1), match.group(2)
    segments = [segment for segment in path.strip('/').split('/') if segment]
    if segments and segments[-1].endswith('.git'):
        segments[-1] = segments[-1][:-len('.git')]
    if not _WEB_HOST.match(host or '') or len(segments) < 2 or not all(_WEB_PATH_SEGMENT.match(segment) for segment in segments):
        return ''

    return 'https://' + host.lower() + '/' + '/'.join(segments)


def _fetched_at(directory: str) -> float | None:
    """The epoch second the repository last fetched (its ``FETCH_HEAD``), or None when it never did."""
    common = run_git(directory, 'rev-parse', '--git-common-dir')
    if not common or not common.strip():
        return None
    path = os.path.join(directory, common.strip(), 'FETCH_HEAD')
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None
