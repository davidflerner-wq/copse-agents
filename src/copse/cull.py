"""Culling: stopping what's left over, and closing workers nobody needs.

A sweep, run by the sidebar every minute, when ``copse`` starts, and by
``copse prune``, does two things:

1. Leftover processes. Any process belonging to an agent that shouldn't be
   running (paused, done, closed, forgotten, or whose tmux window is gone,
   for instance after tmux itself went away) is stopped (see copse.procs). An
   agent whose window vanished while it was marked running is recorded the
   way ``agents.pause`` would have: paused, so ``copse continue`` can still
   bring it back, or done if it had already reported.

2. Stale workers. A worker that reported and has sat idle for ``stale_after``
   minutes (repo config, default 30; 0 turns this off), with nothing queued
   for it, is closed: stopped and hidden from the sidebar. So is a worker
   whose window has been gone that long. Closing never touches its worktree
   or branch, so its supervisor can still review and merge the work.

Interactive agents (supervisor chats) are never closed here; only their
leftover processes are stopped.
"""

from __future__ import annotations

import fcntl
import os
import re
import time

from copse import agents, git, procs, tmux
from copse.config import copse_home, load_repo_config, worktrees_dir
from copse.db import DB, Agent

# An agent this new may not have its window recorded yet.
LAUNCH_GRACE = 60.0


def _root(db: DB, a: Agent) -> str:
    from copse import autopilot

    return autopilot.root_of(db, a.id)


def _stale_after(db: DB, a: Agent, cache: dict[str, float]) -> float:
    ws = db.get_workspace(a.workspace_id)
    if ws is None:
        return 0.0
    if ws.repo_root not in cache:
        try:
            cache[ws.repo_root] = load_repo_config(ws.repo_root).stale_after * 60.0
        except ValueError:
            cache[ws.repo_root] = 0.0
    return cache[ws.repo_root]


def sweep(db: DB, now: float | None = None) -> list[str]:
    """One pass. Returns what it did, one line each."""
    now = time.time() if now is None else now
    panes = tmux.list_panes()
    table = procs.table()
    done: list[str] = []

    # 1. Processes of agents that shouldn't be running.
    leftovers = []
    for aid in procs.all_agent_ids(table):
        a = db.get_agent(aid)
        if a is None or a.status in ("paused", "done") or a.dismissed_at is not None:
            leftovers.append(aid)
        elif (agents.runs_process(a) and not agents.is_alive(a, panes)
              and now - max(a.created_at, a.status_since or 0) > LAUNCH_GRACE):
            db.end_native_subagents(a.id)
            db.set_status(a.id, "done" if a.mode != "interactive" and a.result is not None
                          else "paused")
            leftovers.append(aid)
    if leftovers:
        found = procs.agent_pids(leftovers, procs=table)
        stopped = procs.terminate({p for pids in found.values() for p in pids})
        if stopped:
            done.append(f"stopped {stopped} leftover process(es) of {len(found)} agent(s)")

    # 2. Workers nobody needs any more.
    limits: dict[str, float] = {}
    for a in db.list_agents():
        if (a.mode not in agents.REPORTING_MODES or a.dismissed_at is not None
                or not agents.runs_process(a)):
            continue
        limit = _stale_after(db, a, limits)
        idle_for = now - max(a.created_at, a.status_since or 0)
        if limit <= 0 or idle_for <= limit:
            continue
        alive = agents.is_alive(a, panes)
        finished = alive and a.result is not None and a.status == "idle" and not db.pending_count(a.id)
        # A stopped worker of a paused session comes back with `copse
        # continue`; one whose session carried on without it won't.
        root = db.get_agent(_root(db, a))
        gone = not alive and (a.status != "paused" or root is None or root.status != "paused")
        if finished or gone:
            agents.close(db, a.id, panes)
            done.append(f"closed {'idle' if finished else 'stopped'} worker {a.id} "
                        f"after {int(idle_for // 60)} min")


    # 3. Sidebar locks of sessions that are over.
    removed = clean_locks(db, now)
    if removed:
        done.append(f"removed {removed} stale sidebar lock(s)")
    return done


def clean_locks(db: DB, now: float | None = None) -> int:
    """Delete ``locks/sidebar-<id>.lock`` files whose session root is gone,
    paused or done. Only ones untouched for LAUNCH_GRACE (every use of a lock
    rewrites it) and not held right now, so a launch that's using one can't
    lose it from under it."""
    now = time.time() if now is None else now
    removed = 0
    for path in (copse_home() / "locks").glob("sidebar-*.lock"):
        a = db.get_agent(path.stem.removeprefix("sidebar-"))
        if a is not None and a.status not in ("paused", "done") and a.dismissed_at is None:
            continue
        try:
            if now - path.stat().st_mtime <= LAUNCH_GRACE:
                continue
            with open(path, "a") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# -- `copse prune` ---------------------------------------------------------------


def prune_retired(db: DB, now: float | None = None) -> list[str]:
    """Remove the worktrees of finished workers whose branch is already
    merged (see ``view.retired``). Branches stay, and a worktree with
    uncommitted changes (or the one this is run from) is kept and reported."""
    from copse import view, workspaces

    now = time.time() if now is None else now
    alive = view.live_agents(db, tmux.list_panes())
    cwd = os.path.realpath(os.getcwd())
    done = []
    for ws in db.find_workspaces():
        if not view.retired(db, ws, alive, now):
            continue
        path = os.path.realpath(ws.path)
        if cwd == path or cwd.startswith(path + os.sep):
            done.append(f"kept {ws.branch}: you're in its worktree")
            continue
        try:
            dirty = git.dirty_files(ws.path)
        except git.GitError:
            continue
        if dirty:
            done.append(f"kept {ws.branch}: merged, but {len(dirty)} uncommitted file(s) in {ws.path}")
            continue
        try:
            workspaces.remove(db, ws)
        except (workspaces.WorkspaceError, git.GitError) as e:
            done.append(f"kept {ws.branch}: {e}")
            continue
        done.append(f"removed merged worktree {ws.path} (branch {ws.branch} kept)")
    return done


def orphan_sessions(db: DB) -> list[str]:
    """Kill copse tmux sessions (``copse_*``) that no running agent is in,
    unless someone is attached to them."""
    from copse import view

    panes = tmux.list_panes()
    alive = view.live_agents(db, panes)
    live_panes = {a.tmux_window for a in db.list_agents() if a.id in alive and a.tmux_window}
    live_sessions = {ws.tmux_session for ws in db.find_workspaces()
                     if any(a.id in alive for a in db.list_agents(ws.id))}
    done = []
    for name, attached in tmux.list_sessions():
        if not name.startswith("copse_") or attached or name in live_sessions:
            continue
        if live_panes & set(tmux.session_pane_ids(name)):
            continue
        tmux.kill_session(name)
        done.append(f"killed tmux session {name} (no running agent)")
    return done


def orphan_servers() -> list[str]:
    """Stop leftover private copse tmux servers (``tmux -L copse-*``) and
    remove their sockets: a test run's that outlived the run (it is named
    after the pytest process, see tests/conftest.py), or any whose sessions
    all belong to a COPSE_HOME that no longer exists."""
    done = []
    for name in tmux.other_servers():
        homes = tmux.server_homes(name)
        m = re.fullmatch(r"copse-test-(\d+)", name)
        if homes is None:
            tmux.remove_socket(name)  # nothing listening: just the file
            continue
        if m and not procs.alive(int(m.group(1))):
            reason = "its test run is over"
        elif homes and all(h is None or not os.path.isdir(h) for h in homes):
            reason = "its copse home is gone"
        else:
            continue
        tmux.reap_server(name)
        done.append(f"stopped tmux server {name} ({reason})")
    return done


def empty_worktree_dirs() -> int:
    """Remove empty folders under the worktrees dir (``<repo>/feat`` once its
    last worktree is gone). Returns how many."""
    root = worktrees_dir()
    removed = 0
    for path, _dirs, _files in os.walk(root, topdown=False):
        if os.path.realpath(path) == os.path.realpath(root):
            continue
        try:
            os.rmdir(path)
            removed += 1
        except OSError:
            pass  # not empty
    return removed


def prune(db: DB) -> list[str]:
    """Everything ``copse prune`` cleans up beyond session retention."""
    done = sweep(db)
    done += prune_retired(db)
    done += orphan_sessions(db)
    done += orphan_servers()
    n = empty_worktree_dirs()
    if n:
        done.append(f"removed {n} empty worktree folder(s)")
    return done


def sweep_quietly(db: DB) -> None:
    """A sweep that never raises: culling must never break what calls it."""
    try:
        sweep(db)
    except Exception:  # noqa: BLE001
        pass
