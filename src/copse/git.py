"""Thin, explicit wrappers around the git CLI.

Every call goes through ``run`` so failures surface as ``GitError`` carrying
git's own stderr, and a hung git process can't hang the orchestrator.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT = 120
BASE_CONFIG_KEY = "copse-base"


class GitError(RuntimeError):
    pass


def run(args: list[str], cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def out(args: list[str], cwd: str | Path) -> str:
    return run(args, cwd).stdout.strip()


def ok(args: list[str], cwd: str | Path) -> bool:
    return run(args, cwd, check=False).returncode == 0


# -- repo discovery --------------------------------------------------------


def main_repo_root(path: str | Path) -> str:
    """The main checkout's root, even when ``path`` is inside a linked worktree."""
    common = out(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    common_path = Path(common)
    if common_path.name == ".git":
        return str(common_path.parent)
    # Bare repo or unusual layout: fall back to the current toplevel.
    return out(["rev-parse", "--show-toplevel"], path)


def toplevel(path: str | Path) -> str:
    return out(["rev-parse", "--show-toplevel"], path)


def current_branch(path: str | Path) -> str | None:
    proc = run(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
    return proc.stdout.strip() or None


def default_branch(root: str | Path) -> str:
    proc = run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/", 1)[1]
    for candidate in ("main", "master"):
        if branch_exists(root, candidate):
            return candidate
    return current_branch(root) or "main"


def branch_exists(root: str | Path, branch: str) -> bool:
    return ok(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root)


def remote_branch_exists(root: str | Path, branch: str, remote: str = "origin") -> bool:
    return ok(["show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"], root)


def has_remote(root: str | Path, remote: str = "origin") -> bool:
    return remote in out(["remote"], root).split()


# -- branch naming ---------------------------------------------------------

_INVALID = re.compile(r"[^A-Za-z0-9._/-]+")


def sanitize_branch(name: str, max_len: int = 100) -> str:
    """Make ``name`` a valid, readable branch name."""
    s = _INVALID.sub("-", name.strip())
    s = re.sub(r"/{2,}", "/", s)
    s = re.sub(r"-{2,}", "-", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = "/".join(p.strip(".-") for p in s.split("/") if p.strip(".-"))
    s = s.removesuffix(".lock")[:max_len].strip("/.-")
    if not s:
        raise ValueError(f"cannot derive a branch name from {name!r}")
    return s


def slug(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", branch).strip("-").lower()


# -- worktrees -------------------------------------------------------------


def resolve_start_point(root: str | Path, base: str, fetch: bool) -> str:
    """Where a new branch should start: freshly fetched ``origin/<base>`` when
    it exists, otherwise the local base branch, otherwise HEAD. A failed fetch
    (offline, no auth) is not fatal; we fall back to what we have."""
    if fetch and has_remote(root):
        run(["fetch", "origin", base, "--quiet", "--no-tags"], root, check=False)
    if remote_branch_exists(root, base):
        local_ahead = branch_exists(root, base) and ok(
            ["merge-base", "--is-ancestor", f"origin/{base}", base], root
        )
        # If local base already contains origin (e.g. unpushed local commits),
        # prefer local so those commits aren't silently dropped.
        return base if local_ahead else f"origin/{base}"
    if branch_exists(root, base):
        return base
    return "HEAD"


def worktree_for_branch(root: str | Path, branch: str) -> str | None:
    for wt in list_worktrees(root):
        if wt.get("branch") == f"refs/heads/{branch}":
            return wt["worktree"]
    return None


def add_worktree(root: str | Path, path: str | Path, branch: str, start: str) -> str:
    """Check out ``branch`` at ``path``. Reuses a local or remote branch of that
    name when one exists; otherwise creates it from ``start``.
    Returns ``"existing"``, ``"remote"`` or ``"new"``."""
    elsewhere = worktree_for_branch(root, branch)
    if elsewhere:
        raise GitError(f"branch {branch!r} is already checked out at {elsewhere}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        run(["worktree", "add", str(path), branch], root)
        return "existing"
    if remote_branch_exists(root, branch):
        run(["worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"], root)
        return "remote"
    run(["worktree", "add", "--no-track", "-b", branch, str(path), start], root)
    return "new"


def remove_worktree(root: str | Path, path: str | Path, force: bool = False) -> None:
    args = ["worktree", "remove", str(path)]
    if force:
        args[2:2] = ["--force", "--force"]
    run(args, root)
    run(["worktree", "prune"], root, check=False)


def list_worktrees(root: str | Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out(["worktree", "list", "--porcelain"], root).splitlines():
        if not line:
            if cur:
                result.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or "true"
    if cur:
        result.append(cur)
    return result


def set_base(root: str | Path, branch: str, base: str) -> None:
    run(["config", f"branch.{branch}.{BASE_CONFIG_KEY}", base], root)


def get_base(root: str | Path, branch: str) -> str | None:
    proc = run(["config", "--get", f"branch.{branch}.{BASE_CONFIG_KEY}"], root, check=False)
    return proc.stdout.strip() or None


def delete_branch(root: str | Path, branch: str, force: bool = False) -> None:
    run(["branch", "-D" if force else "-d", branch], root)


# -- status and diff -------------------------------------------------------


@dataclass
class Status:
    branch: str | None
    base: str | None
    ahead: int
    behind: int
    dirty_files: list[str]
    unpushed: int | None   # None when there's no upstream


def base_ref(path: str | Path, base: str) -> str:
    """Compare against ``origin/<base>`` when it exists, else local ``<base>``."""
    return f"origin/{base}" if remote_branch_exists(path, base) else base


def dirty_files(path: str | Path) -> list[str]:
    lines = out(["status", "--porcelain", "--untracked-files=all"], path).splitlines()
    return [line[3:] for line in lines if line]


def status(path: str | Path, base: str | None) -> Status:
    branch = current_branch(path)
    ahead = behind = 0
    if base:
        ref = base_ref(path, base)
        counts = run(["rev-list", "--left-right", "--count", f"{ref}...HEAD"], path, check=False)
        if counts.returncode == 0:
            behind, ahead = (int(x) for x in counts.stdout.split())
    unpushed: int | None = None
    up = run(["rev-list", "--count", "@{upstream}..HEAD"], path, check=False)
    if up.returncode == 0:
        unpushed = int(up.stdout.strip())
    return Status(branch, base, ahead, behind, dirty_files(path), unpushed)


def merge_base(path: str | Path, ref: str) -> str:
    return out(["merge-base", ref, "HEAD"], path)


def diff(path: str | Path, base: str, stat: bool = False) -> str:
    """Everything this branch changes relative to where it forked from
    ``base``: committed work plus uncommitted edits, plus untracked files."""
    mb = merge_base(path, base_ref(path, base))
    args = ["diff", "--stat" if stat else "--patch", mb]
    text = out(args, path)
    untracked = out(["ls-files", "--others", "--exclude-standard"], path).splitlines()
    if untracked:
        text += ("\n" if text else "") + "Untracked files:\n" + "\n".join(
            f"  {f}" for f in untracked
        )
    return text


def commit_all(path: str | Path, message: str) -> str | None:
    run(["add", "-A"], path)
    if ok(["diff", "--cached", "--quiet"], path):
        return None
    run(["commit", "-m", message], path)
    return out(["rev-parse", "--short", "HEAD"], path)


def sync(path: str | Path, base: str, strategy: str = "rebase") -> str:
    """Bring ``base``'s latest commits into this branch. Raises on conflict,
    leaving the rebase/merge in progress so an agent or human can resolve it."""
    if has_remote(path):
        run(["fetch", "origin", base, "--quiet", "--no-tags"], path, check=False)
    ref = base_ref(path, base)
    if strategy == "rebase":
        proc = run(["rebase", ref], path, check=False)
    elif strategy == "merge":
        proc = run(["merge", "--no-edit", ref], path, check=False)
    else:
        raise ValueError(f"unknown strategy {strategy!r}")
    if proc.returncode != 0:
        conflicts = out(["diff", "--name-only", "--diff-filter=U"], path).splitlines()
        raise GitError(
            f"{strategy} onto {ref} stopped with conflicts in: {', '.join(conflicts) or '?'}\n"
            f"Resolve them in {path} and run `git {strategy} --continue`, "
            f"or `git {strategy} --abort`."
        )
    return ref


def merge_into(root: str | Path, target_path: str | Path, branch: str, squash: bool) -> None:
    """Merge ``branch`` into whatever is checked out at ``target_path``."""
    if dirty_files(target_path):
        raise GitError(f"{target_path} has uncommitted changes; commit or stash them first")
    if squash:
        proc = run(["merge", "--squash", branch], target_path, check=False)
        if proc.returncode == 0:
            run(["commit", "--no-edit", "-m", f"Squash merge {branch}"], target_path)
    else:
        proc = run(["merge", "--no-ff", "--no-edit", branch], target_path, check=False)
    if proc.returncode != 0:
        run(["merge", "--abort"], target_path, check=False)
        raise GitError(f"merging {branch} failed (aborted): {proc.stderr.strip() or proc.stdout.strip()}")


def push(path: str | Path, branch: str) -> None:
    run(["push", "--set-upstream", "origin", branch], path)


def remote_web_url(root: str | Path) -> str | None:
    proc = run(["remote", "get-url", "origin"], root, check=False)
    url = proc.stdout.strip()
    m = re.match(r"(?:git@|ssh://git@|https://)([^/:]+)[/:](.+?)(?:\.git)?$", url)
    return f"https://{m.group(1)}/{m.group(2)}" if m else None
