"""Agents: a CLI agent process in a tmux window inside a workspace.

Messaging uses an inbox: a message to a busy agent waits in its
inbox and is delivered the moment the agent goes idle. With hook-capable
providers, delivery happens inside the ``Stop`` hook itself (the hook tells
Claude Code to keep going with the message as its next instruction), so
nothing ever types into a terminal while the agent is mid-turn.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid

from copse import git, tmux, workspaces
from copse.db import DB, Agent, Workspace
from copse.profiles import load_profile
from copse.providers import LaunchContext, get_provider

WORKER_FOOTER = """

---
You are running as a copse worker (agent id {agent_id}) on branch `{branch}`.
When you have finished:
1. Commit your work to this branch with a clear message (do not push or merge).
2. Call the `report_result` tool from the `copse` MCP server with a concise
   summary: what you changed, anything left undone, and anything the
   supervisor should check.
"""


class AgentError(RuntimeError):
    pass


class StillRunning(AgentError):
    """The wait ended before the worker reported; it's still working."""


# Modes whose workers must finish with report_result. A handoff whose caller
# stopped waiting becomes "handoff_detached": its result is then forwarded to
# the caller as a message, like an assign.
REPORTING_MODES = ("handoff", "handoff_detached", "assign")
FORWARDING_MODES = ("handoff_detached", "assign")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def agent_env(ws: Workspace, agent_id: str) -> dict[str, str]:
    return {**workspaces.workspace_env(ws), "COPSE_AGENT_ID": agent_id}


def spawn(
    db: DB,
    ws: Workspace,
    profile_name: str,
    *,
    prompt: str | None = None,
    provider_name: str | None = None,
    parent_id: str | None = None,
    mode: str = "interactive",
    watch_pane: bool = False,
    background_setup: bool = False,
) -> Agent:
    profile = load_profile(profile_name, ws.repo_root)
    provider = get_provider(provider_name or profile.provider)
    agent_id = new_id()

    if prompt and mode in ("handoff", "assign"):
        prompt += WORKER_FOOTER.format(agent_id=agent_id, branch=ws.branch)

    agent = Agent(
        id=agent_id, workspace_id=ws.id, profile=profile.name, provider=provider.name,
        parent_id=parent_id, mode=mode, status="starting", tmux_window="",
        result=None, created_at=time.time(), task=prompt,
    )
    db.add_agent(agent)
    try:
        _launch(db, agent, ws, prompt=prompt, resume=None, watch_pane=watch_pane,
                background_setup=background_setup)
    except Exception:
        db.delete_agent(agent_id)
        raise
    return agent


def _launch(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
            resume: str | None, watch_pane: bool, background_setup: bool = False) -> None:
    """Start (or restart) ``agent``'s CLI in a new tmux window of ``ws``."""
    profile = load_profile(agent.profile, ws.repo_root)
    provider = get_provider(agent.provider)
    # Recorded before launching: the agent's hooks may fire within milliseconds.
    status = "processing" if prompt else "starting"
    if not provider.uses_hooks:
        status = "unknown"
    db.set_status(agent.id, status)
    agent.status = status

    tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    target = tmux.new_window(
        ws.tmux_session, f"{profile.name}-{agent.id[:4]}", ws.path,
        provider.command(LaunchContext(agent.id, profile, prompt, resume=resume)),
        agent_env(ws, agent.id),
    )
    db.update_agent(agent.id, tmux_window=target)
    agent.tmux_window = target
    if agent.mode == "interactive":
        # When this chat ends, pause the session instead of leaving a dead
        # pane behind; see ended().
        from copse.providers import copse_invocation

        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in agent_env(ws, agent.id).items()
                              if k in ("COPSE_HOME", "COPSE_TMUX_SOCKET"))
        cmd = " ".join(shlex.quote(a) for a in [*copse_invocation(), "_ended", agent.id])
        try:
            tmux.on_pane_exit(target, f"{env_prefix} {cmd}".strip())
        except tmux.TmuxError:
            pass
    if watch_pane:
        # The dashboard for this repo, under the agent in the same window.
        # Best effort: a failed split must not fail the agent it sits beside.
        from copse.providers import copse_invocation

        try:
            tmux.split_left(target, ws.path, [*copse_invocation(), "watch", "--sidebar"],
                            workspaces.workspace_env(ws))
        except tmux.TmuxError:
            pass
    tmux.apply_theme(ws.tmux_session)

    if provider.name == "shell" and prompt:
        tmux.paste(target, prompt)
    if background_setup:
        # Startup dialogs (folder trust) are handled by a detached helper so
        # the person gets their terminal immediately.
        from copse.providers import copse_invocation

        subprocess.Popen(
            [*copse_invocation(), "_after-launch", agent.id],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, **agent_env(ws, agent.id)},
        )
    else:
        provider.after_launch(target)


# -- sessions: pause and continue ----------------------------------------------

RESUME_NOTE = (
    "\n\n(You were paused and have just been restarted. Before continuing, check "
    "`git status` and `git log` in your working directory to see what you already did.)"
)


def tree(db: DB, root_id: str) -> list[Agent]:
    """``root_id`` and every agent it started, directly or indirectly."""
    out, queue = [], [root_id]
    while queue:
        a = db.get_agent(queue.pop(0))
        if a is None:
            continue
        out.append(a)
        queue.extend(c.id for c in db.children(a.id))
    return out


def pause(db: DB, root_id: str) -> list[Agent]:
    """Stop a supervisor and everything it started, keeping their work.

    Worktrees, branches, queued messages and each CLI's own session stay; the
    processes stop, so nothing keeps acting while nobody's watching. Agents
    that already reported are left marked done. Returns the agents paused."""
    paused, sessions = [], set()
    for a in tree(db, root_id):
        ws = db.get_workspace(a.workspace_id)
        if ws:
            sessions.add(ws.tmux_session)
        if a.tmux_window:
            # Dead or alive: a dead pane (the chat that just exited) would
            # otherwise hold the window, and the session, open.
            tmux.kill_window(a.tmux_window)
        if a.mode != "interactive" and a.result is not None:
            db.set_status(a.id, "done")
        else:
            db.set_status(a.id, "paused")
            paused.append(a)
    for session in sessions:
        if set(tmux.windows(session)) <= {"shell"}:
            tmux.kill_session(session)
    return paused


def latest_paused(db: DB, ws: Workspace) -> Agent | None:
    """The most recently paused interactive agent (session root) in ``ws``."""
    roots = [a for a in db.list_agents(ws.id) if a.mode == "interactive" and a.status == "paused"]
    return max(roots, key=lambda a: a.status_since or 0, default=None)


def resume(db: DB, root_id: str, *, watch_pane: bool = True) -> list[Agent]:
    """Bring a paused session back: the supervisor and its paused workers
    restart in their own workspaces. Claude Code picks up its previous
    conversation (--resume); other CLIs restart on their original task."""
    resumed = []
    for a in tree(db, root_id):
        if a.status != "paused":
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None or not os.path.isdir(ws.path):
            continue
        provider = get_provider(a.provider)
        ref = a.session_ref if provider.name == "claude" and a.session_ref else None
        if ref and not provider.can_resume(ref):
            ref = None  # nothing was ever said in it: start that agent fresh
        prompt = None if ref else ((a.task + RESUME_NOTE) if a.task else None)
        _launch(db, a, ws, prompt=prompt, resume=ref,
                watch_pane=watch_pane and a.id == root_id, background_setup=True)
        resumed.append(a)
    return resumed


def get(db: DB, agent_id: str) -> Agent:
    agent = db.get_agent(agent_id)
    if not agent:
        # Allow unambiguous prefixes, like git does for hashes.
        matches = [a for a in db.list_agents() if a.id.startswith(agent_id)]
        if len(matches) == 1:
            return matches[0]
        raise AgentError(f"no agent {agent_id!r}")
    return agent


def ended(db: DB, agent_id: str) -> None:
    """An interactive agent's CLI exited (the person closed the chat): pause
    its whole session. The chat's window, dashboard and workers stop; their
    work is kept for `copse --continue`."""
    agent = db.get_agent(agent_id)
    if agent is None or agent.status in ("paused", "done"):
        return
    pause(db, agent_id)


def find_running(db: DB, ws: Workspace, profile: str) -> Agent | None:
    """The newest live interactive agent of ``profile`` in ``ws``, if any."""
    for a in reversed(db.list_agents(ws.id)):
        if a.profile == profile and a.mode == "interactive" and is_alive(a):
            return a
    return None


def is_alive(agent: Agent) -> bool:
    return bool(agent.tmux_window) and tmux.window_alive(agent.tmux_window)


def format_message(db: DB, body: str, sender_id: str | None) -> str:
    if not sender_id:
        return body
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return f"[Message from {who}. Reply with the copse send_message tool, to_agent_id={sender_id}]\n\n{body}"


def send_message(db: DB, to_id: str, body: str, sender_id: str | None = None) -> str:
    """Deliver now if the agent is idle; otherwise queue until it is.
    Returns ``"delivered"`` or ``"queued"``."""
    agent = get(db, to_id)
    if not is_alive(agent):
        raise AgentError(f"agent {agent.id} is not running")
    provider = get_provider(agent.provider)
    text = format_message(db, body, sender_id)
    if not provider.uses_hooks:
        tmux.paste(agent.tmux_window, text)
        return "delivered"
    db.enqueue(agent.id, text, sender_id)
    reconcile(db, agent)
    return "delivered" if flush(db, agent.id) else "queued"


def reconcile(db: DB, agent: Agent, samples: int = 2, gap: float = 0.7) -> Agent:
    """Correct a status the hooks left stale. Claude Code runs no Stop hook
    when a turn is interrupted (Esc), so the agent can sit idle while we still
    think it's busy; and after a permission prompt is approved the status
    stays 'waiting' until the tool finishes. The screen must agree across
    ``samples`` reads before we override the hooks."""
    provider = get_provider(agent.provider)
    if not provider.uses_hooks or agent.status not in ("processing", "waiting"):
        return agent
    seen = set()
    for i in range(samples):
        if i:
            time.sleep(gap)
        try:
            seen.add(provider.screen_state(tmux.capture(agent.tmux_window, lines=40)))
        except tmux.TmuxError:
            return agent
    if len(seen) != 1:
        return agent
    state = seen.pop()
    new = {"idle": "idle", "busy": "processing", "waiting": "waiting"}.get(state or "")
    if new and new != agent.status:
        db.set_status(agent.id, new, only_if=agent.status)
        agent.status = new
    return agent


def flush(db: DB, agent_id: str) -> bool:
    """If the agent is idle, type its oldest pending message. Returns True if
    something was delivered."""
    if db.pending_count(agent_id) == 0 or not db.claim_idle(agent_id):
        return False
    msg = db.pop_pending(agent_id)
    if not msg:
        db.set_status(agent_id, "idle", only_if="processing")
        return False
    agent = db.get_agent(agent_id)
    assert agent is not None
    tmux.paste(agent.tmux_window, msg.body)
    return True


def report_result(db: DB, agent_id: str, result: str) -> str:
    agent = get(db, agent_id)
    db.set_result(agent.id, result)
    if agent.mode in FORWARDING_MODES and agent.parent_id and db.get_agent(agent.parent_id):
        ws = db.get_workspace(agent.workspace_id)
        where = f" on branch `{ws.branch}` (workspace {ws.id})" if ws else ""
        send_message(
            db, agent.parent_id,
            f"Assigned task finished{where}.\n\n{result}",
            sender_id=agent.id,
        )
        return "result recorded and sent to your supervisor"
    return "result recorded"


def wait_for_result(db: DB, agent_id: str, timeout: float, poll: float = 2.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = db.get_agent(agent_id)
        if agent is None:
            raise AgentError(f"agent {agent_id} was removed before reporting")
        if agent.result is not None:
            return agent.result
        if not is_alive(agent):
            screen = ""
            try:
                screen = tmux.capture(agent.tmux_window, lines=40)
            except tmux.TmuxError:
                pass
            raise AgentError(f"agent {agent_id} exited without reporting. Last output:\n{screen}")
        time.sleep(poll)
    agent = db.get_agent(agent_id)
    hint = " It is waiting for a permission approval: attach to its workspace to answer." if agent and agent.status == "waiting" else ""
    raise StillRunning(
        f"agent {agent_id} hasn't reported yet after {int(timeout)}s "
        f"(status: {agent.status if agent else '?'}).{hint}"
    )


def detach(db: DB, agent_id: str) -> str | None:
    """Stop waiting synchronously on a handoff worker: from now on its result
    is forwarded to its parent as a message. Returns the result instead if it
    arrived in the meantime (so it's never lost between the two paths)."""
    db.update_agent(agent_id, mode="handoff_detached")
    agent = db.get_agent(agent_id)
    return agent.result if agent else None


def collect(db: DB, parent_id: str | None, worker_id: str) -> None:
    """The parent received the worker's result directly; drop the duplicate
    copy that forwarding may have queued in the parent's inbox."""
    if parent_id:
        db.drop_pending(parent_id, worker_id)


def kill(db: DB, agent_id: str) -> None:
    agent = get(db, agent_id)
    if agent.tmux_window:
        tmux.kill_window(agent.tmux_window)
    db.delete_agent(agent.id)


# -- delegation (used by the MCP tools) ------------------------------------


def _branch_from_task(profile: str, task: str, agent_hint: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", task.lower())[:5]
    return f"copse/{profile}/{'-'.join(words) or 'task'}-{agent_hint}"


def delegate(
    db: DB,
    caller: Agent | None,
    caller_ws: Workspace,
    profile: str,
    task: str,
    mode: str,
    *,
    isolate: bool = True,
    branch: str | None = None,
) -> tuple[Agent, Workspace]:
    """Start a worker. With ``isolate``, the worker gets a new worktree whose
    branch starts from the caller's current branch, so it sees the caller's
    committed work, and nobody edits the same files."""
    if isolate:
        caller_ws = workspaces.refresh_branch(db, caller_ws)
        base = caller_ws.branch
        branch = branch or _branch_from_task(profile, task, new_id()[:4])
        start = base if git.branch_exists(caller_ws.repo_root, base) else "HEAD"
        created = workspaces.create(
            db, caller_ws.path, branch, base, fetch=False, start=start,
        )
        if created.setup and not created.setup.ok:
            raise AgentError(f"workspace setup failed:\n{created.setup.log}")
        ws = created.workspace
    else:
        ws = caller_ws
    agent = spawn(
        db, ws, profile, prompt=task, parent_id=caller.id if caller else None, mode=mode
    )
    return agent, ws


# -- hook entry point --------------------------------------------------------


def handle_hook(db: DB, agent_id: str, event: str, payload: dict) -> dict | None:
    """Called from ``copse _hook <event>`` inside the agent's own process tree.
    Returns JSON for Claude Code to read on stdout, or None."""
    agent = db.get_agent(agent_id)
    if agent is None:
        return None
    sid = payload.get("session_id")
    if sid and sid != agent.session_ref:
        db.update_agent(agent_id, session_ref=str(sid))

    if event == "session-start":
        db.set_status(agent_id, "idle", only_if="starting")
        if db.pending_count(agent_id):
            # Claude Code hasn't drawn its input box yet; deliver shortly after,
            # from a detached process so this hook returns immediately.
            import subprocess

            from copse.providers import copse_invocation

            subprocess.Popen(
                [*copse_invocation(), "_flush", agent_id, "--delay", "3"],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    elif event == "prompt-submit":
        db.set_status(agent_id, "processing")
    elif event == "notification":
        text = str(payload.get("message", "")).lower()
        if "permission" in text or "approval" in text:
            db.set_status(agent_id, "waiting")
    elif event == "tool-done":
        db.set_status(agent_id, "processing", only_if="waiting")
    elif event == "stop":
        msg = db.pop_pending(agent_id)
        if msg:
            db.set_status(agent_id, "processing")
            return {"decision": "block", "reason": msg.body}
        needs_report = agent.mode in REPORTING_MODES and agent.result is None
        if needs_report and not payload.get("stop_hook_active"):
            db.set_status(agent_id, "processing")
            return {
                "decision": "block",
                "reason": "You haven't called the copse `report_result` tool yet. "
                "If your task is finished, commit your work and call it now. "
                "If you are blocked, call it with a description of what's blocking you.",
            }
        db.set_status(agent_id, "idle")
    return None


def hook_main(db: DB, agent_id: str, event: str, stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    out = handle_hook(db, agent_id, event, payload)
    return json.dumps(out) if out else ""
