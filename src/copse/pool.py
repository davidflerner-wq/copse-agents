"""A pool of pre-built worktrees, so a fresh worker doesn't have to wait for
setup (``pnpm install``, ``uv sync``, ...) inside the ``create`` call.

An entry is a worktree under ``<worktrees_dir>/<repo-slug>/_w/<token>``,
checked out on a placeholder branch (``copse-pool/<token>``) at the base
branch's tip, with the repo's ``copy`` files and ``setup`` already applied.
``workspaces.create`` claims one (renaming its branch to the real one)
instead of running ``git worktree add`` plus setup live, whenever the new
branch would start exactly where a pool entry already sits. A claimed entry's
path and port block never change -- setup ran there for good, so a repo's
``setup`` must not depend on the branch name when ``pool_size`` > 0 (see the
README).

Refilling runs in a detached background process (``copse _pool-fill``),
guarded by an flock so two fills for the same repo don't race. Each fill
first sweeps up anything a crashed fill (or a config change) left behind.
"""

from __future__ import annotations

import fcntl
import filecmp
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from copse import git, workspaces
from copse.config import RepoConfig, load_repo_config, worktrees_dir
from copse.db import DB, PoolEntry, Workspace

POOL_SUBDIR = "_w"
FAILURE_BACKOFF = 600  # seconds to wait before retrying a fill after setup failed

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
    """Build one pool entry from the repo's base branch, at its permanent
    path. Returns ``None`` (after cleaning up) if setup fails."""
    cfg = cfg or load_repo_config(repo_root)
    base = cfg.base_branch or git.default_branch(repo_root)
    base_ref = git.resolve_start_point(repo_root, base, cfg.fetch)
    base_sha = git.out(["rev-parse", base_ref], repo_root)

    token = uuid.uuid4().hex[:8]
    branch = f"copse-pool/{token}"
    path = str(pool_dir(repo_root) / token)
    name = f"pool-{token}"
    port_base = workspaces._next_port_base(db)

    entry = PoolEntry(
        path=path, repo_root=repo_root, base_branch=base, base_sha=base_sha,
        branch=branch, fingerprint=fingerprint(repo_root, base_sha, cfg),
        port_base=port_base, ready=0, created_at=time.time(),
    )
    # Insert before doing any work, so a crash mid-build (worktree add, copy,
    # setup) leaves a row `sweep` can find and clean up.
    db.add_pool_entry(entry)

    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        git.run(["worktree", "add", "--no-track", "-b", branch, path, base_sha], repo_root)
    except git.GitError:
        db.delete_pool_entry(path)
        discard(repo_root, entry)
        return None

    workspaces._copy_local_files(repo_root, path, cfg.copy)
    placeholder = Workspace(
        id=f"{workspaces._repo_slug(repo_root)}/{name}", repo_root=repo_root, name=name,
        kind="worktree", branch=branch, base_branch=base, path=path, port_base=port_base,
        tmux_session=workspaces._session_name(repo_root, name), created_at=entry.created_at,
    )
    setup = (
        workspaces.run_commands(cfg.setup, path, workspaces.workspace_env(placeholder))
        if cfg.setup else None
    )
    if setup is not None and not setup.ok:
        db.delete_pool_entry(path)
        discard(repo_root, entry)
        return None
    db.mark_pool_ready(path)
    entry.ready = 1
    return entry


def fill(db: DB, repo_root: str) -> int:
    """Top the pool back up to the repo's configured ``pool_size``. Returns
    how many entries were added. Backs off for a while after a setup failure
    instead of retrying (and failing) on every trigger."""
    cfg = load_repo_config(repo_root)
    target = cfg.pool_size or 0
    if target <= 0:
        return 0
    base = cfg.base_branch or git.default_branch(repo_root)
    failed_at = db.last_pool_failure(repo_root, base)
    if failed_at is not None and time.time() - failed_at < FAILURE_BACKOFF:
        return 0
    added = 0
    while db.count_pool_entries(repo_root, base) < target:
        if fill_one(db, repo_root, cfg) is None:
            db.record_pool_failure(repo_root, base)
            break
        db.clear_pool_failure(repo_root, base)
        added += 1
    return added


def _lock_path(repo_root: str) -> Path:
    return pool_dir(repo_root).parent / ".pool.lock"


def fill_locked(db: DB, repo_root: str) -> int:
    """Sweep, trim, then ``fill``, skipping entirely if another fill for this
    repo is already running. Uses an flock rather than a stale-time heuristic,
    so a lock is only ever released by the process (or OS) that holds it."""
    lock = _lock_path(repo_root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            sweep(db, repo_root)
            trim(db, repo_root)
            return fill(db, repo_root)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def fill_in_background(repo_root: str) -> None:
    """Start ``copse _pool-fill <repo_root>`` detached, so refilling (and its
    sweep/trim) never blocks the caller (a claim, or supervisor start)."""
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


def rebind(repo_root: str, entry: PoolEntry, branch: str, start_sha: str) -> None:
    """Turn a claimed entry into workspace ``branch``, in place: the entry's
    path and port never change. Only resets the worktree forward when the
    base moved past the entry's own base_sha, and only ever on the entry's
    own placeholder branch -- callers only claim when ``branch`` doesn't
    already exist locally or on origin, so this can never discard real work.
    """
    if entry.base_sha != start_sha:
        git.run(["checkout", "-B", entry.branch, start_sha], entry.path)
    git.run(["branch", "-m", entry.branch, branch], entry.path)


def discard(repo_root: str, entry: PoolEntry) -> None:
    """Best-effort cleanup of an entry that's being dropped."""
    if os.path.exists(entry.path):
        git.run(["worktree", "remove", "--force", entry.path], repo_root, check=False)
    git.run(["branch", "-D", entry.branch], repo_root, check=False)
    git.run(["worktree", "prune"], repo_root, check=False)
    if os.path.exists(entry.path):
        shutil.rmtree(entry.path, ignore_errors=True)


# -- cleanup ------------------------------------------------------------------


def sweep(db: DB, repo_root: str, cfg: RepoConfig | None = None) -> None:
    """Clean up anything a crashed fill (or a config change since the last
    one) could have left behind: not-ready rows (safe to call this stale
    only because it runs under this repo's fill lock, which guarantees no
    other fill for this repo is running right now), entries for a base
    branch that's no longer configured, pool directories with no matching
    row, and placeholder branches with no worktree."""
    cfg = cfg or load_repo_config(repo_root)
    current_base = cfg.base_branch or git.default_branch(repo_root)

    for e in db.pool_entries(repo_root, ready_only=False):
        if not e.ready or e.base_branch != current_base:
            db.delete_pool_entry(e.path)
            discard(repo_root, e)

    known_paths = {e.path for e in db.pool_entries(repo_root, ready_only=False)}
    base_dir = pool_dir(repo_root)
    if base_dir.is_dir():
        for child in base_dir.iterdir():
            if str(child) not in known_paths:
                git.run(["worktree", "remove", "--force", str(child)], repo_root, check=False)
                shutil.rmtree(child, ignore_errors=True)
    git.run(["worktree", "prune"], repo_root, check=False)

    live_branches = {
        wt["branch"].removeprefix("refs/heads/")
        for wt in git.list_worktrees(repo_root) if "branch" in wt
    }
    for b in git.list_branches(repo_root, "copse-pool/*"):
        if b not in live_branches:
            git.run(["branch", "-D", b], repo_root, check=False)


def trim(db: DB, repo_root: str) -> int:
    """Drop ready entries beyond the repo's current ``pool_size`` (e.g. after
    it was lowered, or disabled). Returns how many were removed."""
    cfg = load_repo_config(repo_root)
    target = max(0, cfg.pool_size or 0)
    base = cfg.base_branch or git.default_branch(repo_root)
    entries = db.pool_entries(repo_root, base)
    excess = entries[: max(0, len(entries) - target)]
    for e in excess:
        db.delete_pool_entry(e.path)
        discard(repo_root, e)
    return len(excess)
