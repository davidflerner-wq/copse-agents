"""What `copse ls` and `copse watch` show: one entry per workspace, with its
agents. Git fields are None when they can't be computed."""

from __future__ import annotations

import os

from copse import agents, git
from copse.db import DB, Agent, Workspace


def workspace_entry(db: DB, ws: Workspace, *, detail: bool = False) -> dict:
    ahead = behind = dirty = None
    if ws.base_branch and os.path.isdir(ws.path):
        try:
            st = git.status(ws.path, ws.base_branch)
            ahead, behind, dirty = st.ahead, st.behind, len(st.dirty_files)
        except git.GitError:
            pass
    return {
        "id": ws.id,
        "name": ws.name,
        "branch": ws.branch,
        "base_branch": ws.base_branch,
        "path": ws.path,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "agents": [agent_entry(db, a, detail=detail) for a in db.list_agents(ws.id)],
    }


def agent_entry(db: DB, a: Agent, *, detail: bool = False) -> dict:
    if not agents.runs_process(a):
        status = a.status  # a supervisor's own subagent: no terminal to check
    elif agents.is_alive(a):
        a = agents.reconcile(db, a, samples=1)
        status = a.status
    else:
        status = "exited"
    entry = {"id": a.id, "profile": a.profile, "provider": a.provider,
             "status": status, "mode": a.mode}
    if detail:
        entry.update(
            parent_id=a.parent_id,
            status_since=a.status_since,
            pending=db.pending_count(a.id),
            reported=a.result is not None,
            window=a.tmux_window,
            headless=bool(a.headless),
        )
    return entry


def snapshot(db: DB, repo_root: str | None) -> list[dict]:
    return [workspace_entry(db, ws, detail=True) for ws in db.find_workspaces(repo_root)]


def autopilot_entry(db: DB, repo_root: str | None) -> dict | None:
    """The goal and milestones of the newest running autopilot session in
    ``repo_root``, for the sidebar."""
    from copse import autopilot

    if not repo_root:
        return None
    roots = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
             if a.mode == "interactive" and a.status not in ("paused", "done")
             and db.get_autopilot(a.id) and agents.is_alive(a)]
    if not roots:
        return None
    root = max(roots, key=lambda a: a.created_at)
    ap = db.get_autopilot(root.id)
    assert ap is not None
    return {
        "enabled": bool(ap.enabled),
        "goal": ap.goal,
        "state": ap.state,
        "note": ap.note,
        "milestones": [{"position": m.position, "title": m.title, "status": m.status,
                        "check": m.check_cmd} for m in db.milestones(root.id)],
        "usage": autopilot.usage(),
        "workers": len(autopilot.active_workers(db, root.id)),
    }
