import os
import time

import pytest

from copse import agents, git, sessions, workspaces
from copse.db import Agent


def add(db, ws, aid, mode="interactive", parent=None, status="paused", since=None, result=None):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", result, time.time(), since or time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    return workspaces.adopt_root(db, str(repo))


def test_keeps_only_the_newest_three(db, root):
    for i in range(5):
        add(db, root, f"s{i}", since=1000 + i)
    assert sessions.enforce(db, root.repo_root, now=1100) == 2
    assert [s.root.id for s in sessions.paused(db, root.repo_root)] == ["s4", "s3", "s2"]


def test_drops_sessions_older_than_a_week(db, root):
    now = time.time()
    add(db, root, "fresh", since=now - 3600)
    add(db, root, "stale", since=now - 8 * 86400)
    sessions.enforce(db, root.repo_root, now=now)
    assert [s.root.id for s in sessions.paused(db, root.repo_root)] == ["fresh"]


def test_forget_removes_clean_worktrees_keeps_dirty_ones_and_all_branches(db, root, repo):
    clean = workspaces.create(db, str(repo), "clean-work").workspace
    dirty = workspaces.create(db, str(repo), "dirty-work").workspace
    open(os.path.join(clean.path, "done.txt"), "w").write("x")
    git.commit_all(clean.path, "finished")
    open(os.path.join(dirty.path, "wip.txt"), "w").write("x")  # uncommitted
    add(db, root, "boss", since=1)
    add(db, clean, "w1", mode="assign", parent="boss", result="ok")
    add(db, dirty, "w2", mode="assign", parent="boss")
    for i in range(3):
        add(db, root, f"new{i}", since=100 + i)  # push "boss" past KEEP

    sessions.enforce(db, root.repo_root, now=200)
    assert db.get_agent("boss") is None and db.get_agent("w1") is None
    assert not os.path.exists(clean.path) and os.path.exists(dirty.path)
    assert git.branch_exists(str(repo), "clean-work") and git.branch_exists(str(repo), "dirty-work")
    assert git.out(["log", "-1", "--format=%s", "main"], str(repo)) == "init"  # nothing merged


def test_pause_then_resume_uses_the_saved_claude_session(db, root, monkeypatch, tmp_path):
    saved = tmp_path / "claude" / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-boss.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append((a.id, kw)))
    add(db, root, "boss")
    add(db, root, "w1", mode="assign", parent="boss")
    add(db, root, "w2", mode="assign", parent="boss", status="done", result="ok")
    agents.resume(db, "boss")
    assert [a for a, _ in launched] == ["boss", "w1"]  # finished workers stay done
    assert launched[0][1]["resume"] == "sess-boss" and launched[0][1]["prompt"] is None
    assert launched[0][1]["watch_pane"] and not launched[1][1]["watch_pane"]


def test_hook_records_the_cli_session_id(db, root):
    add(db, root, "a1", status="idle")
    agents.handle_hook(db, "a1", "prompt-submit", {"session_id": "abc-123"})
    assert db.get_agent("a1").session_ref == "abc-123"


def test_resume_starts_fresh_when_claude_never_saved_the_chat(db, root, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    add(db, root, "boss")
    agents.resume(db, "boss")
    assert launched[0]["resume"] is None
    (tmp_path / "claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / "claude" / "projects" / "p" / "sess-boss.jsonl").write_text("{}")
    db.set_status("boss", "paused")
    agents.resume(db, "boss")
    assert launched[1]["resume"] == "sess-boss"
