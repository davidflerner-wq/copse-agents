"""MCP server each agent gets as ``copse``. It knows which agent is calling
from ``COPSE_AGENT_ID`` (set in the agent's environment at spawn)."""

from __future__ import annotations

import asyncio
import os

from mcp.server.mcpserver import MCPServer

from copse import agents, git, workspaces
from copse.db import DB, Agent, Workspace
from copse.profiles import list_profiles

MAX_DIFF_CHARS = 60_000

mcp = MCPServer(
    "copse",
    instructions=(
        "copse runs other coding agents for you, each on its own git branch in its own "
        "worktree. Delegate with `handoff` (wait for the result) or `assign` (continue "
        "working; the result arrives later as a message). Review a worker's branch with "
        "`workspace_diff`, integrate it with `merge_workspace`, clean up with "
        "`remove_workspace`. Workers must finish by calling `report_result`."
    ),
)


def _caller(db: DB) -> tuple[Agent | None, Workspace]:
    agent_id = os.environ.get("COPSE_AGENT_ID")
    agent = db.get_agent(agent_id) if agent_id else None
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return agent, ws
    ws = workspaces.current(db) or workspaces.adopt_root(db, os.getcwd())
    return agent, ws


def _ws(db: DB, ref: str) -> Workspace:
    _, here = _caller(db)
    return workspaces.resolve(db, ref, cwd=here.path)


def _summary(db: DB, ws: Workspace) -> str:
    base = ws.base_branch
    if not base:
        return f"branch {ws.branch}"
    st = git.status(ws.path, base)
    stat = git.diff(ws.path, base, stat=True) or "(no changes)"
    dirty = f", {len(st.dirty_files)} uncommitted file(s)" if st.dirty_files else ""
    return (
        f"workspace {ws.id} on branch {ws.branch}: {st.ahead} commit(s) ahead of {base}, "
        f"{st.behind} behind{dirty}\n{stat}"
    )


DEFAULT_WAIT_SECONDS = 240


def _await_worker(db: DB, worker_id: str, wait_seconds: int) -> str:
    """Wait a bounded time for a handoff worker. Tool calls must stay short:
    MCP clients time out long calls, and a timed-out call would strand the
    result. On timeout the worker is detached, so its result is forwarded as
    a message later, and the caller can resume with wait_for_worker."""
    worker = agents.get(db, worker_id)
    wws = db.get_workspace(worker.workspace_id)
    isolated = bool(wws and wws.kind == "worktree")
    try:
        result = agents.wait_for_result(db, worker.id, wait_seconds)
    except agents.StillRunning as e:
        result = agents.detach(db, worker.id)
        if result is None:
            return (
                f"Worker {worker.id} is still running ({e}). Its result will arrive as a "
                f"message when it finishes. To keep waiting now instead, call "
                f"wait_for_worker(agent_id=\"{worker.id}\")."
            )
    except agents.AgentError as e:
        return f"Worker {worker.id} failed: {e}"
    agents.collect(db, worker.parent_id, worker.id)
    if worker.mode.startswith("handoff"):
        # The caller has the result; assign workers stay up for feedback.
        agents.kill(db, worker.id)
    tail = f"\n\n{_summary(db, wws)}" if isolated and wws else ""
    return f"Worker {worker.id} finished.\n\n{result}{tail}"


@mcp.tool()
async def handoff(
    agent_profile: str, task: str, isolate: bool = True, branch: str | None = None,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
) -> str:
    """Give a task to a new worker agent and wait for its result.

    Waits up to wait_seconds (default 4 minutes). If the worker isn't done by
    then, this returns "still running": call wait_for_worker to keep waiting,
    or carry on, and the result will arrive as a message.

    With isolate=true (default) the worker gets its own git worktree on a new
    branch cut from YOUR current branch. It only sees work you've committed,
    so commit first if the worker needs your latest changes. The result
    includes the worker's summary and a diffstat of its branch; review with
    workspace_diff and integrate with merge_workspace. Pass a short
    descriptive branch (e.g. "fix/login-redirect") to name the worker's branch.
    With isolate=false the worker shares your working directory; only use that
    for read-only tasks.
    """
    def run() -> str:
        db = DB()
        caller, ws = _caller(db)
        worker, _ = agents.delegate(
            db, caller, ws, agent_profile, task, "handoff", isolate=isolate, branch=branch
        )
        return _await_worker(db, worker.id, wait_seconds)

    return await asyncio.to_thread(run)


@mcp.tool()
async def wait_for_worker(agent_id: str, wait_seconds: int = DEFAULT_WAIT_SECONDS) -> str:
    """Keep waiting for a worker started with handoff that was still running.
    Returns its result, or "still running" again after wait_seconds."""
    def run() -> str:
        db = DB()
        return _await_worker(db, agent_id, wait_seconds)

    return await asyncio.to_thread(run)


@mcp.tool()
async def assign(
    agent_profile: str, task: str, isolate: bool = True, branch: str | None = None
) -> str:
    """Start a worker agent on a task and return immediately.

    When it finishes, its result arrives in your conversation as a message.
    Isolation works as for handoff. Use this to run several workers in parallel.
    Pass a short descriptive branch (e.g. "feat/ls-json") to name the worker's branch.
    """
    def run() -> str:
        db = DB()
        caller, ws = _caller(db)
        worker, wws = agents.delegate(
            db, caller, ws, agent_profile, task, "assign", isolate=isolate, branch=branch
        )
        return f"Started worker {worker.id} ({worker.profile}) in workspace {wws.id} on branch {wws.branch}."

    return await asyncio.to_thread(run)


@mcp.tool()
def send_message(to_agent_id: str, message: str) -> str:
    """Send a message to another agent. It's delivered when that agent is idle."""
    db = DB()
    caller, _ = _caller(db)
    return agents.send_message(db, to_agent_id, message, caller.id if caller else None)


@mcp.tool()
def report_result(result: str) -> str:
    """Workers: call this once when your task is done (after committing), with
    a concise summary of what you did and anything left to check."""
    db = DB()
    caller, _ = _caller(db)
    if not caller:
        return "Not running as a copse agent; nothing to report to."
    return agents.report_result(db, caller.id, result)


@mcp.tool()
def list_agents() -> str:
    """List agents in this repository with their status, workspace and branch."""
    db = DB()
    caller, here = _caller(db)
    lines = []
    for ws in db.find_workspaces(here.repo_root):
        for a in db.list_agents(ws.id):
            me = " (you)" if caller and a.id == caller.id else ""
            parent = f" parent={a.parent_id}" if a.parent_id else ""
            lines.append(
                f"{a.id}{me} {a.profile}/{a.provider} {a.status} mode={a.mode}{parent} "
                f"ws={ws.id} branch={ws.branch}"
            )
    return "\n".join(lines) or "No agents."


@mcp.tool()
def list_agent_profiles() -> str:
    """Agent profiles available to handoff/assign."""
    db = DB()
    _, here = _caller(db)
    return "\n".join(f"{p.name} ({p.provider}): {p.description}" for p in list_profiles(here.repo_root))


@mcp.tool()
def workspace_diff(workspace: str, stat_only: bool = False) -> str:
    """Show everything a workspace's branch changes relative to its base
    (commits plus uncommitted edits)."""
    db = DB()
    ws = _ws(db, workspace)
    text = git.diff(ws.path, workspaces.require_base(ws), stat=stat_only) or "(no changes)"
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (truncated; use stat_only or read files directly)"
    return f"{_summary(db, ws)}\n\n{text}" if not stat_only else _summary(db, ws)


@mcp.tool()
def merge_workspace(workspace: str, squash: bool = False) -> str:
    """Merge a workspace's branch into its base branch (for workers: your branch).
    Fails without changing anything on conflicts."""
    db = DB()
    ws = _ws(db, workspace)
    target = workspaces.merge_back(db, ws, squash=squash)
    return f"Merged {ws.branch} into {ws.base_branch} at {target}."


@mcp.tool()
def remove_workspace(workspace: str, delete_branch: bool = False, force: bool = False) -> str:
    """Remove a workspace's worktree and stop its agents. The branch is kept
    unless delete_branch=true (which only deletes it if merged, unless force)."""
    db = DB()
    ws = _ws(db, workspace)
    removed = workspaces.remove(db, ws, force=force, delete_branch=delete_branch)
    return f"Removed {ws.id}. {removed.branch_note or 'branch deleted'}"


def main() -> None:
    mcp.run()
