"""Starting a supervisor on another branch/worktree, and handing a session over."""

import json
import time

import pytest

from copse import agents, autopilot, config, git, sessions, workspaces
from copse.db import Agent, Task

from conftest import sh


def add(db, ws, aid, mode="interactive", parent=None, status="running"):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", None, time.time(), time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


def test_config_is_found_from_a_linked_worktree_via_the_main_one(repo, tmp_path):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.local.json").write_text(json.dumps({"max_agents": 7}))
    linked = tmp_path / "linked"
    sh(f"git worktree add -q -b other {linked}", repo)
    assert not (linked / ".copse").exists()
    assert config.load_repo_config(linked).max_agents == 7
    assert config.config_root(linked) == repo


def test_checkout_for_branch_creates_then_reuses_the_worktree(db, repo):
    ws = workspaces.checkout_for(db, str(repo), branch="integration")
    assert ws.branch == "integration" and ws.path != str(repo)
    again = workspaces.checkout_for(db, str(repo), branch="integration")
    assert again.id == ws.id


def test_checkout_for_adopts_a_worktree_the_user_made(db, repo, tmp_path):
    mine = tmp_path / "mine"
    sh(f"git worktree add -q -b topic {mine}", repo)
    ws = workspaces.checkout_for(db, str(repo), branch="topic")
    assert ws.path == str(mine.resolve()) and ws.repo_root == str(repo)
    assert workspaces.checkout_for(db, str(repo), worktree=str(mine)).id == ws.id


def test_checkout_for_worktree_path_creates_it_with_the_branch(db, repo, tmp_path):
    path = tmp_path / "wt" / "feature"
    ws = workspaces.checkout_for_target(db, str(repo), str(path))
    assert ws.path == str(path.resolve()) and git.current_branch(path) == "feature"
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), worktree=str(tmp_path / "nope"))


def test_checkout_for_rejects_a_worktree_on_another_branch(db, repo, tmp_path):
    mine = tmp_path / "mine"
    sh(f"git worktree add -q -b topic {mine}", repo)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.checkout_for(db, str(repo), branch="other", worktree=str(mine))


def test_handover_moves_goal_workers_tasks_and_carries_the_note(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    dest = workspaces.checkout_for(db, str(repo), branch="integration")
    add(db, root, "old")
    worker = workspaces.create(db, str(repo), "w-branch").workspace
    add(db, worker, "w1", mode="assign", parent="old")
    db.add_autopilot("old")
    autopilot.set_goal(db, "old", "ship it", [("one", "true", None), ("two", "false", "d", "reviewer")], "why")
    m1 = db.milestones("old")[0]
    db.record_check(m1.id, True, "ok", "abc")
    db.add_task(Task("t1", str(repo), None, "old", root.id, "developer", "later", "assign", 1, None,
                     None, None, '["w1"]', "pending", time.time()))

    spawned = {}

    def fake_spawn(db_, ws, profile, **kw):
        spawned.update(kw, ws=ws)
        a = add(db_, ws, "new")
        if kw["autopilot"]:
            db_.add_autopilot("new")
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    paused = []
    monkeypatch.setattr(agents, "pause", lambda db_, rid, **kw: paused.append(rid))

    new = sessions.handover(db, "old", dest, "rule: never push to main")

    assert new.id == "new" and spawned["ws"].id == dest.id and paused == ["old"]
    assert "rule: never push to main" in spawned["prompt"] and "w1" in spawned["prompt"]
    ap = db.get_autopilot("new")
    assert ap.goal == "ship it" and ap.detail == "why"
    ms = db.milestones("new")
    assert [(m.title, m.status, m.profile) for m in ms] == [("one", "passed", None), ("two", "pending", "reviewer")]
    assert db.get_agent("w1").parent_id == "new"
    task = db.get_task("t1")
    assert task.caller_id == "new" and task.caller_ws_id == dest.id
    assert not db.get_autopilot("old").enabled


def test_checkout_for_target_reads_a_slashed_name_as_a_branch(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    ws = workspaces.checkout_for_target(db, str(repo), "fix/handover")
    assert ws.branch == "fix/handover" and not (repo / "fix").exists()
