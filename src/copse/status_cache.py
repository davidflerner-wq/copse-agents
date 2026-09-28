"""A cheaper ``git.status`` for the dashboard and ``copse ls``.

``git.status`` shells out several times per workspace (current branch, base
ref resolution, two rev-lists, dirty files). Redrawn every 2s across N
workspaces, that adds up. This module:

- resolves and caches ``base_ref`` per workspace for the process lifetime
  (it rarely changes once a workspace exists), and
- caches the rest of the status behind a cheap fingerprint (mtimes of the
  worktree's index and HEAD files, plus the base ref's mtime/sha), so a
  workspace nothing has touched costs zero git subprocesses on a redraw.

The cache is process-lifetime, module-level state: correct for one
long-lived ``copse watch``/``copse ls`` process, not meant to be shared
across processes.
"""

from __future__ import annotations

import time
from pathlib import Path

from copse import git

TTL = 30.0  # safety net: recompute at least this often even if nothing else invalidated the cache

_base_ref_cache: dict[tuple[str, str], str] = {}
_status_cache: dict[str, tuple[tuple, float, git.Status]] = {}


def clear() -> None:
    """Drop all cached state. Mainly for tests."""
    _base_ref_cache.clear()
    _status_cache.clear()


def _cached_base_ref(path: Path, base: str) -> str:
    key = (str(path), base)
    ref = _base_ref_cache.get(key)
    if ref is None:
        ref = git.base_ref(path, base)
        _base_ref_cache[key] = ref
    return ref


def _worktree_gitdir(path: Path) -> Path:
    """The worktree's private gitdir: ``path/.git`` itself for a plain repo,
    or wherever its ``.git`` file points for a linked worktree."""
    dotgit = path / ".git"
    if dotgit.is_dir():
        return dotgit
    text = dotgit.read_text()
    return Path(text.split(":", 1)[1].strip())


def _common_gitdir(worktree_gitdir: Path) -> Path:
    """The shared gitdir (holds refs/objects) a linked worktree's private
    gitdir points back to, read from its ``commondir`` file. Avoids a
    ``git rev-parse --git-common-dir`` subprocess."""
    commondir_file = worktree_gitdir / "commondir"
    if not commondir_file.is_file():
        return worktree_gitdir
    rel = commondir_file.read_text().strip()
    return (worktree_gitdir / rel).resolve()


def _ref_file(common_gitdir: Path, ref: str) -> Path:
    if ref.startswith("origin/"):
        return common_gitdir / "refs" / "remotes" / ref
    return common_gitdir / "refs" / "heads" / ref


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _fingerprint(path: Path, ref: str) -> tuple[float, float, float, float]:
    wt_gitdir = _worktree_gitdir(path)
    common = _common_gitdir(wt_gitdir)
    index_mtime = _mtime(wt_gitdir / "index")
    head_mtime = _mtime(wt_gitdir / "HEAD")
    ref_mtime = _mtime(_ref_file(common, ref))
    if ref_mtime == 0.0:
        ref_mtime = _mtime(common / "packed-refs")
    # A file appearing/disappearing/renaming anywhere under the worktree root
    # touches that directory's mtime (though not its own parent's, if nested
    # deeper); catches the common "new untracked file at the top level" case
    # cheaply. Editing an existing tracked file in place touches neither this
    # nor the index, so that case waits for the TTL safety net.
    root_mtime = _mtime(path)
    return (index_mtime, head_mtime, ref_mtime, root_mtime)


def _parse_branch_header(line: str) -> tuple[str | None, bool, int]:
    """Parses ``git status --porcelain=v1 --branch``'s first line.
    Returns ``(branch, has_upstream, ahead_of_upstream)``."""
    body = line[3:]  # strip "## "
    if body.startswith("No commits yet on "):
        return body[len("No commits yet on "):], False, 0
    if body == "HEAD (no branch)":
        return None, False, 0
    branch, sep, rest = body.partition("...")
    if not sep:
        return branch, False, 0
    _upstream, _, bracket = rest.partition(" [")
    bracket = bracket.rstrip("]")
    if not bracket or bracket == "gone":
        return branch, bracket != "gone", 0
    ahead = 0
    for part in bracket.split(", "):
        if part.startswith("ahead"):
            ahead = int(part.split()[1])
    return branch, True, ahead


def _compute_status(path: Path, base: str, ref: str) -> git.Status:
    proc = git.run(["status", "--porcelain=v1", "--branch", "--untracked-files=all"], path)
    lines = proc.stdout.splitlines()
    branch = None
    unpushed: int | None = None
    file_lines = lines
    if lines and lines[0].startswith("## "):
        branch, has_upstream, ahead_of_upstream = _parse_branch_header(lines[0])
        unpushed = ahead_of_upstream if has_upstream else None
        file_lines = lines[1:]
    dirty = [line[3:] for line in file_lines if line]
    behind, ahead = git.ahead_behind(path, ref)
    return git.Status(branch, base, ahead, behind, dirty, unpushed)


def cached_status(path: str | Path, base: str | None, now: float | None = None) -> git.Status:
    """Like ``git.status``, but backed by the module-level cache above."""
    if not base:
        return git.status(path, base)
    path = Path(path)
    now = time.time() if now is None else now
    ref = _cached_base_ref(path, base)
    try:
        fp = _fingerprint(path, ref)
    except OSError:
        return _compute_status(path, base, ref)
    key = str(path)
    hit = _status_cache.get(key)
    if hit is not None:
        cached_fp, cached_at, cached_status_ = hit
        if cached_fp == fp and now - cached_at < TTL:
            return cached_status_
    st = _compute_status(path, base, ref)
    # `git status` can rewrite the index (refreshing racily-clean stat data)
    # even when nothing changed, so re-fingerprint *after* computing: caching
    # the pre-call fingerprint would make every call look stale.
    try:
        fp = _fingerprint(path, ref)
    except OSError:
        return st
    _status_cache[key] = (fp, now, st)
    return st
