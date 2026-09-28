"""Task coordination between parallel workers.

``assign``/``handoff`` may declare ``files`` (paths/globs a task expects to
touch) and ``depends_on`` (earlier tasks, by agent id or branch name, that
must be merged first). A task with unmet dependencies is recorded here as
'pending' (no worker started yet) and started once ``merge_workspace``
resolves its dependencies, cut from the updated base branch. A task with
declared ``files`` is checked against other active workers' declared and
actually-changed files, so the caller learns about likely collisions without
being blocked by them.
"""

from __future__ import annotations

import fnmatch
import json
import time
from pathlib import PurePath

from copse import agents, git
from copse.db import DB, Agent, Task, Workspace


def new_id() -> str:
    return agents.new_id()


def _dumps(items: list[str] | None) -> str | None:
    return json.dumps(list(items)) if items else None


def _loads(text: str | None) -> list[str]:
    return json.loads(text) if text else []


# -- overlap warnings ---------------------------------------------------------


def _glob_match(a: str, b: str) -> bool:
    """Whether globs/paths ``a`` and ``b`` could refer to the same file(s)."""
    if a == b:
        return True
    if fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a, b):
        return True
    if "**" in a or "**" in b:
        try:
            return PurePath(b).match(a) or PurePath(a).match(b)
        except ValueError:
            return False
    return False


def _changed_files(ws: Workspace) -> list[str]:
    """Files ``ws``'s branch has touched relative to its base, cheaply (no
    process beyond a couple of git calls)."""
    if not ws.base_branch:
        return []
    try:
        mb = git.merge_base(ws.path, git.base_ref(ws.path, ws.base_branch))
        names = git.out(["diff", "--name-only", mb], ws.path)
        untracked = git.out(["ls-files", "--others", "--exclude-standard"], ws.path)
    except git.GitError:
        return []
    return [f for f in (*names.splitlines(), *untracked.splitlines()) if f]


def active_tasks(db: DB, repo_root: str, *, exclude_agent_id: str | None = None) -> list[Task]:
    """Started tasks in ``repo_root`` whose worker hasn't reported yet."""
    out = []
    for t in db.list_tasks(repo_root, state="started"):
        if not t.agent_id or t.agent_id == exclude_agent_id:
            continue
        agent = db.get_agent(t.agent_id)
        if agent and agent.result is None:
            out.append(t)
    return out


def overlap_warning(db: DB, ws: Workspace, files: list[str] | None) -> str | None:
    """A warning if ``files`` overlaps another active task's declared or
    actually-changed files, or None. Still starts the worker regardless: this
    is informational, not a block."""
    if not files:
        return None
    for t in active_tasks(db, ws.repo_root):
        other_agent = db.get_agent(t.agent_id) if t.agent_id else None
        other_ws = db.get_workspace(other_agent.workspace_id) if other_agent else None
        candidates = list(_loads(t.files))
        if other_ws:
            candidates += _changed_files(other_ws)
        for mine in files:
            for theirs in candidates:
                if _glob_match(mine, theirs):
                    branch = other_ws.branch if other_ws else "?"
                    return (f"overlaps with {t.agent_id} ({branch}) on {theirs}; "
                            "consider depends_on or merging first")
    return None


# -- dependencies ---------------------------------------------------------------


def _dep_branch(db: DB, dep: str) -> str:
    """A dependency identifier's branch: ``dep`` may be an agent id (its
    workspace's branch) or already a branch name."""
    agent = db.get_agent(dep)
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return ws.branch
    return dep


def unmet_dependencies(db: DB, caller_ws: Workspace, depends_on: list[str] | None) -> list[str]:
    """``depends_on`` entries not yet merged into ``caller_ws``'s branch."""
    if not depends_on:
        return []
    return [dep for dep in depends_on
            if not git.ok(["merge-base", "--is-ancestor", _dep_branch(db, dep), "HEAD"], caller_ws.path)]


def _dep_matches(dep: str, ws: Workspace, worker_id: str | None) -> bool:
    return dep == ws.branch or (worker_id is not None and dep == worker_id)


# -- queueing and starting -------------------------------------------------------


def enqueue(
    db: DB, caller: Agent | None, caller_ws: Workspace, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None,
) -> Task:
    """Record a task that can't start yet: no worker, no workspace, just what
    it takes to start it once its dependencies are merged."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=None,
        caller_id=caller.id if caller else None, caller_ws_id=caller_ws.id,
        profile=profile, task_text=task_text, mode=mode, isolate=int(isolate),
        branch=branch, done_when=done_when, files=_dumps(files),
        depends_on=_dumps(depends_on), state="pending", created_at=time.time(),
    )
    db.add_task(t)
    return t


def record_started(
    db: DB, caller_ws: Workspace, worker: Agent, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None,
) -> Task:
    """Record a task that started right away, so its ``files`` can be checked
    for overlap against later tasks."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=worker.id,
        caller_id=worker.parent_id, caller_ws_id=caller_ws.id, profile=profile,
        task_text=task_text, mode=mode, isolate=int(isolate), branch=branch,
        done_when=done_when, files=_dumps(files), depends_on=_dumps(depends_on),
        state="started", created_at=time.time(), started_at=time.time(),
    )
    db.add_task(t)
    return t


def start_queued(db: DB, task: Task) -> Agent:
    """Start a queued task now that its dependencies are met, cutting its
    branch from the caller workspace's current (updated) base."""
    caller_ws = db.get_workspace(task.caller_ws_id)
    if caller_ws is None:
        raise agents.AgentError(f"workspace {task.caller_ws_id} for queued task {task.id} is gone")
    caller = db.get_agent(task.caller_id) if task.caller_id else None
    worker, _wws = agents.delegate(
        db, caller, caller_ws, task.profile, task.task_text, task.mode,
        isolate=bool(task.isolate), branch=task.branch, done_when=task.done_when,
    )
    db.update_task(task.id, agent_id=worker.id, state="started", started_at=time.time())
    return worker


def on_merged(db: DB, ws: Workspace) -> None:
    """``ws``'s branch was just merged into its base: start any pending task
    that was only waiting on it and now has every dependency merged, and tell
    its caller."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    for t in db.list_tasks(ws.repo_root, state="pending"):
        deps = _loads(t.depends_on)
        if not any(_dep_matches(d, ws, worker.id if worker else None) for d in deps):
            continue
        caller_ws = db.get_workspace(t.caller_ws_id)
        if not caller_ws or unmet_dependencies(db, caller_ws, deps):
            continue
        try:
            new_worker = start_queued(db, t)
        except agents.AgentError:
            continue
        if t.caller_id:
            try:
                agents.send_message(
                    db, t.caller_id, f"Started {new_worker.id} (was waiting on {dep_ref}).",
                    sender_id=None,
                )
            except agents.AgentError:
                pass


def on_removed_unmerged(db: DB, ws: Workspace) -> None:
    """``ws`` was removed while its branch still had commits not in its base:
    cancel any pending task depending on it, and tell its caller."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    for t in db.list_tasks(ws.repo_root, state="pending"):
        deps = _loads(t.depends_on)
        if not any(_dep_matches(d, ws, worker.id if worker else None) for d in deps):
            continue
        db.update_task(t.id, state="cancelled")
        if t.caller_id:
            try:
                agents.send_message(
                    db, t.caller_id,
                    f"Cancelled queued task {t.id}: it was waiting on {dep_ref}, which was "
                    "removed unmerged.",
                    sender_id=None,
                )
            except agents.AgentError:
                pass


def list_text(db: DB, repo_root: str) -> str:
    """Pending and cancelled tasks: started ones already show in list_agents."""
    lines = []
    for t in db.list_tasks(repo_root):
        if t.state == "started":
            continue
        deps = _loads(t.depends_on)
        parts = [t.id, t.state, t.profile, f"branch={t.branch or '(auto)'}"]
        if deps:
            parts.append(f"depends_on={','.join(deps)}")
        if t.files:
            parts.append(f"files={','.join(_loads(t.files))}")
        lines.append(" ".join(parts))
    return "\n".join(lines) or "No pending or cancelled tasks."
