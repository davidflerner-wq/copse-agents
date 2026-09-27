"""Scratch sessions: copse outside a git repository.

Running `copse` somewhere that isn't a git repo (your home directory, a
downloads folder) starts a scratch session: a fresh git repo under
``~/.copse/scratch/`` that tracks the work the same way a real repo would.
Nothing is created where you ran copse. Later, ``transfer`` replays the
scratch session's commits onto a new branch of a real repository.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from copse import git, workspaces
from copse.config import copse_home
from copse.db import DB, Workspace

ORIGIN_FILE = "copse-origin"            # inside .git: the folder copse was run from
TRANSFERRED_FILE = "copse-transferred"  # inside .git: where the work was moved to
BRANCH = "main"


class ScratchError(RuntimeError):
    pass


def scratch_dir() -> Path:
    return copse_home() / "scratch"


def is_scratch(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(scratch_dir().resolve())
        return True
    except ValueError:
        return False


def _git_dir(root: str | Path) -> Path:
    return Path(root) / ".git"


def origin_of(root: str | Path) -> str | None:
    f = _git_dir(root) / ORIGIN_FILE
    return f.read_text().strip() if f.is_file() else None


def transferred_to(root: str | Path) -> str | None:
    f = _git_dir(root) / TRANSFERRED_FILE
    return f.read_text().strip() if f.is_file() else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "session"


def create(db: DB, origin: str) -> Workspace:
    """A new scratch repo, with one empty commit so workers can branch from it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = scratch_dir() / f"{_slug(Path(origin).name)}-{stamp}"
    root, n = base, 2
    while root.exists():  # two sessions started within the same second
        root, n = base.with_name(f"{base.name}-{n}"), n + 1
    root.mkdir(parents=True)
    git.run(["init", "-q", "-b", BRANCH], root)
    # Use the person's own git identity when they have one; otherwise a local
    # placeholder, so the first commit never fails on a fresh machine.
    ident: list[str] = []
    if not git.ok(["config", "user.email"], root):
        ident = ["-c", "user.name=copse", "-c", "user.email=copse@localhost"]
    git.run([*ident, "commit", "-q", "--allow-empty", "-m", "Start copse scratch session"], root)
    (_git_dir(root) / ORIGIN_FILE).write_text(str(Path(origin).resolve()) + "\n")
    return workspaces.adopt_root(db, str(root))


def sessions(db: DB) -> list[Workspace]:
    """Scratch sessions copse knows about, newest first."""
    out = [w for w in db.find_workspaces() if w.kind == "main" and is_scratch(w.path)
           and os.path.isdir(w.path)]
    return sorted(out, key=lambda w: w.created_at, reverse=True)


def for_origin(db: DB, origin: str) -> Workspace | None:
    """The newest untransferred scratch session started from ``origin``."""
    origin = str(Path(origin).resolve())
    for ws in sessions(db):
        if origin_of(ws.path) == origin and not transferred_to(ws.path):
            return ws
    return None


def commit_count(ws: Workspace) -> int:
    """Commits made in the session, not counting the empty starting commit."""
    out = git.out(["rev-list", "--count", BRANCH], ws.path)
    return max(0, int(out) - 1)


def pending(db: DB) -> list[Workspace]:
    """Sessions with work that hasn't been moved into a real repo yet."""
    return [ws for ws in sessions(db)
            if not transferred_to(ws.path)
            and (commit_count(ws) > 0 or git.dirty_files(ws.path))]


@dataclass
class Transferred:
    workspace: Workspace
    commits: int
    snapshot: bool      # uncommitted work was committed first


def transfer(db: DB, scratch: Workspace, target: str, branch: str | None = None) -> Transferred:
    """Replay the scratch session's commits onto a new branch of ``target``,
    as a normal copse workspace (worktree + branch cut from the repo's base).
    The scratch repo is left in place, marked as transferred."""
    if not is_scratch(scratch.path):
        raise ScratchError(f"{scratch.id} is not a scratch session")
    try:
        target_root = git.main_repo_root(os.path.expanduser(target))
    except git.GitError:
        raise ScratchError(
            f"{target} isn't inside a git repository. To make it one, run "
            f"`git init` there first (copse won't do that for you)."
        ) from None
    if is_scratch(target_root):
        raise ScratchError("the target is itself a scratch session; pick a real repository")

    snapshot = False
    if git.dirty_files(scratch.path):
        git.commit_all(scratch.path, "Work in progress from copse scratch session")
        snapshot = True
    commits = commit_count(scratch)
    if commits == 0:
        raise ScratchError("this scratch session has no work to transfer yet")

    name = branch or f"copse/from-{Path(scratch.path).name}"
    created = workspaces.create(db, target_root, name, run_setup=True)
    ws = created.workspace
    root_commit = git.out(["rev-list", "--max-parents=0", BRANCH], scratch.path).splitlines()[-1]
    git.run(["fetch", "-q", "--no-tags", str(scratch.path), BRANCH], ws.path)
    tip = git.out(["rev-parse", "FETCH_HEAD"], ws.path)
    proc = git.run(["cherry-pick", "--allow-empty", f"{root_commit}..{tip}"], ws.path, check=False)
    if proc.returncode != 0:
        conflicts = git.out(["diff", "--name-only", "--diff-filter=U"], ws.path).splitlines()
        raise ScratchError(
            f"the scratch work conflicts with {target_root} in: {', '.join(conflicts) or '?'}. "
            f"Resolve it in {ws.path} and run `git cherry-pick --continue` "
            f"(or `git cherry-pick --abort`)."
        )
    (_git_dir(scratch.path) / TRANSFERRED_FILE).write_text(f"{target_root} {ws.branch}\n")
    return Transferred(ws, commits, snapshot)
