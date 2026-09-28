"""What `copse ls` and `copse watch` show: one entry per workspace, with its
agents. Git fields are None when they can't be computed."""

from __future__ import annotations

import logging
import os
import time

from copse import agents, git, status_cache, tmux
from copse import usage as usage_mod
from copse.db import DB, NATIVE_SUBAGENT_STALE, Agent, NativeSubagent, Workspace

log = logging.getLogger(__name__)

# How long a *finished* native subagent still shows "done" in the sidebar
# before disappearing entirely. Display-only, so it lives here rather than
# with NATIVE_SUBAGENT_STALE/_PRUNE_AFTER in db.py, which govern the table
# itself (when a "running" row counts as crashed, and when rows are dropped).
NATIVE_SUBAGENT_LINGER = 30
# Parent states where a subagent can never really be "running" any more:
# a paused or killed parent's own SubagentStop hooks never fire, and
# end_native_subagents (called from agents.pause/kill) may not have caught
# up yet, so this is a display-side backstop.
_PARENT_NOT_RUNNING = ("paused", "exited", "done")


def workspace_entry(db: DB, ws: Workspace, *, detail: bool = False,
                    native_subagents: dict[str, list[NativeSubagent]] | None = None,
                    now: float | None = None,
                    panes: dict[str, bool] | None = None) -> dict:
    ahead = behind = dirty = None
    if ws.base_branch and os.path.isdir(ws.path):
        try:
            st = status_cache.cached_status(ws.path, ws.base_branch, now=now)
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
        "agents": [
            agent_entry(db, a, detail=detail,
                       native_subagents=None if native_subagents is None else native_subagents.get(a.id, []),
                       now=now, panes=panes)
            for a in db.list_agents(ws.id)
        ],
    }


def _visible_native_subagents(subs: list[NativeSubagent], now: float) -> list[dict]:
    out = []
    for s in subs:
        if s.ended_at is None:
            if now - s.started_at > NATIVE_SUBAGENT_STALE:
                continue
        elif now - s.ended_at > NATIVE_SUBAGENT_LINGER:
            continue
        out.append({"id": s.id, "agent_type": s.agent_type, "started_at": s.started_at,
                    "ended_at": s.ended_at})
    return out


def agent_entry(db: DB, a: Agent, *, detail: bool = False,
                native_subagents: list[NativeSubagent] | None = None,
                now: float | None = None,
                panes: dict[str, bool] | None = None) -> dict:
    # Usage is a display extra: a bad transcript must never break the sidebar.
    try:
        u = usage_mod.agent_usage(db, a)
    except Exception:
        log.exception("copse: couldn't read usage for %s", a.id)
        u = None
    if not agents.runs_process(a):
        status = a.status  # a supervisor's own subagent: no terminal to check
    elif agents.is_alive(a, panes):
        a = agents.reconcile(db, a, samples=1)
        status = a.status
    else:
        status = "exited"
    entry = {"id": a.id, "profile": a.profile, "provider": a.provider,
             "status": status, "mode": a.mode}
    if u and u.total:
        entry["tokens"] = usage_mod.short_summary(u)
    if detail:
        subs = db.native_subagents(a.id) if native_subagents is None else native_subagents
        visible = _visible_native_subagents(subs, now if now is not None else time.time())
        if status in _PARENT_NOT_RUNNING:
            visible = [s for s in visible if s["ended_at"] is not None]
        entry.update(
            parent_id=a.parent_id,
            status_since=a.status_since,
            pending=db.pending_count(a.id),
            reported=a.result is not None,
            window=a.tmux_window,
            headless=bool(a.headless),
            subagents=visible,
        )
    return entry


def snapshot(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> list[dict]:
    now = time.time()
    by_parent = db.all_native_subagents()
    if panes is None:
        panes = tmux.list_panes()
    return [workspace_entry(db, ws, detail=True, native_subagents=by_parent, now=now, panes=panes)
            for ws in db.find_workspaces(repo_root)]


def autopilot_entry(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> dict | None:
    """The goal and milestones of the newest running autopilot session in
    ``repo_root``, for the sidebar. ``panes`` should be the same
    ``tmux.list_panes()`` result passed to ``snapshot`` for this refresh, so
    liveness isn't checked with a second tmux subprocess."""
    from copse import autopilot

    if not repo_root:
        return None
    roots = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
             if a.mode == "interactive" and a.status not in ("paused", "done")
             and db.get_autopilot(a.id) and agents.is_alive(a, panes)]
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
        "workers": len(autopilot.working_workers(db, root.id)),
    }
