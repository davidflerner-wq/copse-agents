"""The pipeline's cleanup can't kill its own reviewer, and it won't merge into
the default branch unless told to."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, gates, pipeline, tmux, workspaces
from copse.db import Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", None,
                       time.time(), **kw))


def setup(db, repo, monkeypatch, config):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"review": True, **config}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    db.update_agent("w1", result="added new.py", pipeline="reviewing")
    add(db, ws, "rev0", "review", "reviewer", parent="boss", status="processing")
    db.add_review(ws.id, gates.head(ws), "rev0", True, "lgtm")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return ws


def test_cleanup_tells_first_and_keeps_the_reviewers_session(db, repo, monkeypatch):
    ws = setup(db, repo, monkeypatch, {"auto_merge_default_branch": True})
    events = []
    monkeypatch.setattr(tmux, "kill_session", lambda s: events.append(("kill", s)))
    real_tell, real_remove = pipeline._tell, workspaces.remove
    monkeypatch.setattr(pipeline, "_tell", lambda *a, **k: (events.append("tell"), real_tell(*a, **k)))
    monkeypatch.setattr(workspaces, "remove",
                        lambda *a, **k: (events.append(("remove", k.get("keep_session"))),
                                         real_remove(*a, **k))[1])
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    assert events == ["tell", ("remove", True)]                # no session was killed
    assert "Merged feat into main" in db.pop_pending("boss").body
    assert db.get_workspace(ws.id) is None


def test_a_removal_failure_still_leaves_the_message_and_status(db, repo, monkeypatch):
    ws = setup(db, repo, monkeypatch, {"auto_merge_default_branch": True})

    def boom(*a, **k):
        raise workspaces.WorkspaceError("teardown failed")

    monkeypatch.setattr(workspaces, "remove", boom)
    pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm")
    assert db.get_agent("w1").pipeline is None and db.get_agent("rev0").status == "done"
    bodies = [db.pop_pending("boss").body, db.pop_pending("boss").body]
    assert any("Merged feat" in b for b in bodies) and any("worktree kept" in b for b in bodies)


def test_default_branch_is_not_merged_into_without_opt_in(db, repo, monkeypatch):
    ws = setup(db, repo, monkeypatch, {})
    assert pipeline.on_review(db, db.get_agent("rev0"), ws, True, "lgtm") is True
    assert "new.py" not in sh("git ls-tree --name-only HEAD", repo)
    body = db.pop_pending("boss").body
    assert "needs you" in body and "merge_workspace" in body
    assert db.get_workspace(ws.id) is not None and db.get_agent("w1").pipeline is None


def test_merge_into_puts_workers_on_that_branch_and_is_merged_there(db, repo, monkeypatch):
    sh("git branch integration", repo)
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"merge_into": "integration"}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        agents.delegate(db, db.get_agent("boss"), root, "developer", "do it", "assign")
    ws = [w for w in db.find_workspaces() if w.kind == "worktree"][0]
    assert ws.base_branch == "integration"


def test_merge_into_leaves_a_workers_own_sub_workers_on_its_branch(db, repo, monkeypatch):
    sh("git branch integration", repo)
    sh("git branch topic", repo)
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"merge_into": "integration"}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    mine = workspaces.create(db, str(repo), "topic", apply_prefix=False).workspace
    add(db, mine, "dev", "assign", parent="boss", status="processing")
    monkeypatch.setattr(agents, "spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        agents.delegate(db, db.get_agent("dev"), mine, "developer", "sub task", "assign")
    ws = [w for w in db.find_workspaces() if w.kind == "worktree" and w.id != mine.id][0]
    assert ws.base_branch == "topic"
