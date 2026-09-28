"""A pool of pre-built worktrees, so a fresh worker doesn't have to wait for
setup (``pnpm install``, ``uv sync``, ...) inside the ``create`` call.

An entry is a worktree under ``<worktrees_dir>/<repo-slug>/_pool/<token>``,
checked out on a placeholder branch (``copse-pool/<token>``) at the base
branch's tip, with the repo's ``copy`` files and ``setup`` already applied.
``workspaces.create`` claims one (moving it into place and rebinding it to
the real branch) instead of running ``git worktree add`` plus setup live,
whenever the new branch would start exactly where a pool entry already sits.

Refilling runs in a detached background process (``copse _pool-fill``),
guarded by a lock file so two fills for the same repo don't race.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from copse import git, workspaces
from copse.config import RepoConfig, load_repo_config, worktrees_dir
from copse.db import DB, PoolEntry

POOL_SUBDIR = "_pool"
LOCK_STALE = 300  # seconds; a fill that's held the lock this long is assumed dead

LOCKFILES = ("uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
             "Cargo.lock", "poetry.lock", "go.sum")


def pool_dir(repo_root: str) -> Path:
    return worktrees_dir() / workspaces._repo_slug(repo_root) / POOL_SUBDIR


def fingerprint(repo_root: str, sha: str, cfg: RepoConfig) -> str:
    """A hash of the setup commands plus the contents of any common lockfile
    present at ``sha``, so a dependency change invalidates a pool entry that
    was built at an older commit."""
    h = hashlib.sha256()
    for cmd in cfg.setup:
        h.update(cmd.encode())
        h.update(b"\0")
    for name in LOCKFILES:
        proc = git.run(["show", f"{sha}:{name}"], repo_root, check=False)
        if proc.returncode == 0:
            h.update(name.encode())
            h.update(proc.stdout.encode())
            h.update(b"\0")
    return h.hexdigest()


# -- filling -----------------------------------------------------------------


def fill_one(db: DB, repo_root: str, cfg: RepoConfig | None = None) -> PoolEntry | None:
    """Build one pool entry from the repo's base branch. Returns ``None``
    (after cleaning up) if setup fails."""
    cfg = cfg or load_repo_config(repo_root)
    base = cfg.base_branch or git.default_branch(repo_root)
    base_ref = git.resolve_start_point(repo_root, base, cfg.fetch)
    base_sha = git.out(["rev-parse", base_ref], repo_root)

    token = uuid.uuid4().hex[:8]
    branch = f"copse-pool/{token}"
    path = str(pool_dir(repo_root) / token)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    git.run(["worktree", "add", "--no-track", "-b", branch, path, base_sha], repo_root)

    entry = PoolEntry(
        path=path, repo_root=repo_root, base_branch=base, base_sha=base_sha,
        branch=branch, fingerprint=fingerprint(repo_root, base_sha, cfg), created_at=time.time(),
    )
    workspaces._copy_local_files(repo_root, path, cfg.copy)
    env = {
        "COPSE_ROOT_PATH": repo_root, "COPSE_WORKSPACE_PATH": path,
        "COPSE_BRANCH": branch, "COPSE_BASE_BRANCH": base,
    }
    setup = workspaces.run_commands(cfg.setup, path, env) if cfg.setup else None
    if setup is not None and not setup.ok:
        discard(repo_root, entry)
        return None
    db.add_pool_entry(entry)
    return entry


def fill(db: DB, repo_root: str) -> int:
    """Top the pool back up to the repo's configured ``pool_size``. Returns
    how many entries were added."""
    cfg = load_repo_config(repo_root)
    target = cfg.pool_size or 0
    if target <= 0:
        return 0
    base = cfg.base_branch or git.default_branch(repo_root)
    added = 0
    while db.count_pool_entries(repo_root, base) < target:
        if fill_one(db, repo_root, cfg) is None:
            break
        added += 1
    return added


def _lock_path(repo_root: str) -> Path:
    return pool_dir(repo_root).parent / ".pool.lock"


def fill_locked(db: DB, repo_root: str) -> int:
    """``fill``, skipping entirely if another fill for this repo is already
    running (a lock held longer than ``LOCK_STALE`` is taken over as dead)."""
    lock = _lock_path(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            stale = time.time() - lock.stat().st_mtime > LOCK_STALE
        except OSError:
            return 0
        if not stale:
            return 0
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        except OSError:
            return 0
    os.close(fd)
    try:
        return fill(db, repo_root)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def fill_in_background(repo_root: str) -> None:
    """Start ``copse _pool-fill <repo_root>`` detached, so refilling never
    blocks the caller (a claim, or supervisor start)."""
    from copse.providers import copse_invocation

    try:
        subprocess.Popen(
            [*copse_invocation(), "_pool-fill", repo_root],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# -- claiming ----------------------------------------------------------------


def claim(db: DB, repo_root: str, base: str) -> PoolEntry | None:
    return db.take_pool_entry(repo_root, base)


def move_into_place(repo_root: str, entry: PoolEntry, path: str, branch: str, base_sha: str) -> None:
    git.move_worktree(repo_root, entry.path, path)
    git.run(["checkout", "-B", branch, base_sha], path)
    git.delete_branch(repo_root, entry.branch, force=True)


def discard(repo_root: str, entry: PoolEntry, dest: str | None = None) -> None:
    """Best-effort cleanup of an entry that's being dropped: whether it's
    still at its own path, or was already (partially) moved to ``dest``."""
    paths = {entry.path, dest} - {None}
    for p in paths:
        if os.path.exists(p):
            git.run(["worktree", "remove", "--force", p], repo_root, check=False)
    git.run(["branch", "-D", entry.branch], repo_root, check=False)
    git.run(["worktree", "prune"], repo_root, check=False)
    for p in paths:
        if os.path.exists(p):
            shutil.rmtree(p, ignore_errors=True)


# -- cleanup ------------------------------------------------------------------


def trim(db: DB, repo_root: str) -> int:
    """Drop entries beyond the repo's current ``pool_size`` (e.g. after it was
    lowered, or disabled). Returns how many were removed."""
    cfg = load_repo_config(repo_root)
    target = max(0, cfg.pool_size or 0)
    base = cfg.base_branch or git.default_branch(repo_root)
    entries = db.pool_entries(repo_root, base)
    excess = entries[: max(0, len(entries) - target)]
    for e in excess:
        db.delete_pool_entry(e.path)
        discard(repo_root, e)
    return len(excess)
