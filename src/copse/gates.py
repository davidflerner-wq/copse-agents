"""Merge gates: what must hold before an agent may merge a worker's branch.

Checked by the ``merge_workspace`` MCP tool, in the worker's worktree:
1. Everything is committed.
2. A reviewer agent approved this exact commit (``review`` in the repo config;
   by default only in autopilot sessions). New commits need a new review.
3. pre-commit (the framework) passes over the branch's changes, when the repo
   has a ``.pre-commit-config.yaml``. Plain git hooks already ran when each
   commit was made.
4. Every command in ``checks`` exits 0.

copse runs these itself, so "done" means verified, not just claimed. People
merging with ``copse merge`` aren't gated: that's their own call.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from copse import autopilot, git, workspaces
from copse.config import RepoConfig
from copse.db import DB, Workspace


@dataclass
class Report:
    ok: bool = True
    passed: list[str] = field(default_factory=list)
    problem: str | None = None
    sha: str | None = None        # the commit the gates checked

    def fail(self, problem: str) -> "Report":
        self.ok, self.problem = False, problem
        return self

    def summary(self) -> str:
        return "; ".join(self.passed) if self.passed else "no gates configured"


def head(ws: Workspace) -> str:
    return git.out(["rev-parse", "HEAD"], ws.path)


def run_checked(db: DB, ws: Workspace, cmd: str, env: dict[str, str], timeout: int) -> tuple[bool, str]:
    """Run ``cmd`` in ``ws``, or reuse the cached result for the same (workspace,
    HEAD sha, command) if the tree was clean when that result was cached. A
    dirty tree always runs fresh and is never cached, since the result then
    reflects more than just the commit at ``sha``."""
    sha = head(ws)
    dirty = bool(git.dirty_files(ws.path))
    if not dirty:
        cached = db.get_check(ws.id, sha, cmd)
        if cached is not None:
            return bool(cached.ok), cached.output or ""
    ok, out = autopilot.run_check(cmd, ws.path, env, timeout)
    if not dirty:
        db.set_check(ws.id, sha, cmd, ok, out)
    return ok, out


def check_summary(db: DB, ws: Workspace, cfg: RepoConfig) -> str:
    """Run each of ``cfg.checks`` (cached by sha) and produce a short pass/fail
    summary for a reviewer, with output only for the ones that failed."""
    if not cfg.checks:
        return ""
    env = workspaces.workspace_env(ws)
    lines = []
    for cmd in cfg.checks:
        ok, out = run_checked(db, ws, cmd, env, cfg.check_timeout)
        lines.append(f"PASS `{cmd}`" if ok else f"FAIL `{cmd}`\n{out}")
    return "\n".join(lines)


def run(db: DB, ws: Workspace, cfg: RepoConfig, *, review_required: bool) -> Report:
    r = Report()
    dirty = git.dirty_files(ws.path)
    if dirty:
        shown = ", ".join(dirty[:8]) + (" ..." if len(dirty) > 8 else "")
        return r.fail(f"{ws.name} has uncommitted changes ({shown}). The worker must commit them first.")

    sha = r.sha = head(ws)
    if review_required:
        review = db.latest_review(ws.id, sha)
        if review is None:
            return r.fail(
                f"{ws.branch} at {sha[:8]} hasn't been reviewed. Call request_review "
                f"(workspace=\"{ws.id}\") and merge once it approves."
            )
        if not review.approved:
            return r.fail(
                f"The review of {ws.branch} at {sha[:8]} requested changes:\n{review.summary or ''}\n"
                "Send these to the worker, then request another review."
            )
        r.passed.append(f"review approved {sha[:8]}")

    if cfg.pre_commit and (problem := _pre_commit(db, ws, cfg)):
        return r.fail(problem)
    if cfg.pre_commit and _has_pre_commit(ws) and shutil.which("pre-commit"):
        r.passed.append("pre-commit passed")

    env = workspaces.workspace_env(ws)
    for cmd in cfg.checks:
        ok, out = run_checked(db, ws, cmd, env, cfg.check_timeout)
        if not ok:
            return r.fail(f"Check failed in {ws.branch}:\n{out}\nSend this to the worker to fix.")
        r.passed.append(f"`{cmd}` passed")
    return r


def _has_pre_commit(ws: Workspace) -> bool:
    from pathlib import Path

    return (Path(ws.path) / ".pre-commit-config.yaml").is_file()


def _pre_commit(db: DB, ws: Workspace, cfg: RepoConfig) -> str | None:
    """Run pre-commit over the files the branch changes. Returns a problem, or None."""
    if not _has_pre_commit(ws) or not shutil.which("pre-commit"):
        return None
    base = workspaces.require_base(ws)
    start = git.merge_base(ws.path, git.base_ref(ws.path, base))
    ok, out = run_checked(
        db, ws, f"pre-commit run --from-ref {start} --to-ref HEAD",
        workspaces.workspace_env(ws), cfg.check_timeout,
    )
    if ok:
        return None
    return f"pre-commit hooks failed on {ws.branch}:\n{out}\nSend this to the worker to fix."
