"""The subagent provider: copse manages the workspace, the supervisor's own
Agent tool does the work."""

import asyncio
import os
import time

import pytest

from conftest import sh
from copse import agents, mcp_server, tmux, view, workspaces
from copse.db import Agent
from copse.profiles import load_profile


@pytest.fixture
def boss(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


def _started(db) -> Agent:
    [worker] = [a for a in db.list_agents() if a.provider == "subagent"]
    return worker


def test_builtin_subagent_profile():
    p = load_profile("subagent")
    assert p.provider == "subagent" and p.prompt


def test_handoff_makes_a_workspace_and_starts_nothing(db, boss, monkeypatch):
    def no_tmux(*a, **k):
        raise AssertionError("a subagent worker gets no tmux window")

    monkeypatch.setattr(tmux, "new_window", no_tmux)
    monkeypatch.setattr(tmux, "ensure_session", no_tmux)
    t0 = time.time()
    out = asyncio.run(mcp_server.handoff("subagent", "Add a greeting to app.py", branch="feat/greet",
                                         done_when="python app.py prints hello"))
    assert time.time() - t0 < 30  # doesn't wait for the work
    worker = _started(db)
    ws = db.get_workspace(worker.workspace_id)
    assert ws.kind == "worktree" and ws.branch == "feat/greet" and ws.base_branch == "main"
    assert os.path.isdir(ws.path)
    assert worker.parent_id == "boss" and worker.mode == "handoff"
    assert worker.tmux_window == "" and worker.status == "processing"
    # The reply says exactly what to do next.
    for piece in (ws.path, "feat/greet", worker.id, "complete_subagent", "Agent tool",
                  f'workspace_diff("{ws.id}")'):
        assert piece in out
    prompt = out.split("----- prompt for your Agent tool -----\n")[1].split("\n----- end of prompt")[0]
    assert prompt == worker.task
    assert "Add a greeting to app.py" in prompt and "python app.py prints hello" in prompt
    assert f"cd {ws.path} && " in prompt and "commit" in prompt.lower()
    assert "report_result" not in prompt  # the subagent shares the supervisor's copse tools


def test_subagent_is_working_until_completed_then_merges_and_removes(db, boss, repo):
    out = asyncio.run(mcp_server.assign("subagent", "Add a file", branch="feat/file"))
    worker = _started(db)
    ws = db.get_workspace(worker.workspace_id)
    assert "complete_subagent" in out

    # Liveness: no process, yet it's working, not dead.
    assert agents.is_alive(worker)
    entry = view.agent_entry(db, worker, detail=True)
    assert entry["status"] == "processing" and entry["provider"] == "subagent"
    assert "complete_subagent" in mcp_server._await_worker(db, worker.id, wait_seconds=0)

    # copse can't message it; say so rather than fail obscurely.
    with pytest.raises(agents.AgentError, match="Agent tool"):
        agents.send_message(db, worker.id, "hello")

    # The subagent does its work in the worktree and commits.
    sh("echo new > file.txt && git add file.txt && git commit -qm 'add file'", ws.path)
    reply = mcp_server.complete_subagent(worker.id, "Added file.txt")
    assert "Recorded" in reply and "1 commit(s) ahead" in reply
    done = db.get_agent(worker.id)
    assert done.result == "Added file.txt" and done.status == "done"
    assert not agents.is_alive(done)
    assert view.agent_entry(db, done)["status"] == "done"
    assert "Added file.txt" in mcp_server._await_worker(db, worker.id, wait_seconds=0)
    assert db.pending_count("boss") == 0  # nothing forwarded to the caller's own inbox

    assert "file.txt" in mcp_server.workspace_diff(ws.id)
    merged = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert merged.startswith("Merged feat/file into main"), merged
    assert (repo / "file.txt").read_text() == "new\n"
    removed = mcp_server.remove_workspace(ws.id, delete_branch=True)
    assert removed.startswith("Removed")
    assert db.get_agent(worker.id) is None and not os.path.exists(ws.path)


def test_complete_subagent_refuses_process_workers(db, boss):
    db.add_agent(Agent("w1", boss.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    assert "report_result" in mcp_server.complete_subagent("w1", "done")
    assert db.get_agent("w1").result is None


def test_subagent_profile_only_works_through_delegation(db, boss):
    with pytest.raises(agents.AgentError, match="handoff or assign"):
        agents.spawn(db, boss, "subagent", prompt="x")
    with pytest.raises(agents.AgentError, match="handoff or assign"):
        agents.spawn(db, boss, "subagent", prompt="x", mode="review")
    assert [a.id for a in db.list_agents()] == ["boss"]


def test_pause_and_resume_keep_subagent_records(db, boss, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    monkeypatch.setattr(tmux, "kill_session", lambda s: None)
    monkeypatch.setattr(tmux, "windows", lambda s: [])
    asyncio.run(mcp_server.assign("subagent", "Open task", branch="feat/open"))
    open_one = _started(db)
    agents.pause(db, "boss")
    assert db.get_agent(open_one.id).status == "paused"
    assert not agents.is_alive(db.get_agent(open_one.id))

    launched = []
    real_launch = agents._launch

    def launch(db_, a, ws, **kw):
        if a.provider == "subagent":
            return real_launch(db_, a, ws, **kw)
        launched.append(a.id)

    monkeypatch.setattr(agents, "_launch", launch)
    agents.resume(db, "boss")
    assert launched == ["boss"]
    assert db.get_agent(open_one.id).status == "processing"
