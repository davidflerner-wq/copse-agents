import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, autopilot, gates, workspaces
from copse.config import RepoConfig
from copse.db import Agent
from copse.profiles import load_profile


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def add_worker(db, ws, agent_id, task=None, done_when=None, mode="handoff"):
    a = Agent(agent_id, ws.id, "developer", "claude", None, mode, "done", "@0", "did stuff",
              time.time(), task=task, done_when=done_when)
    db.add_agent(a)
    return a


def count_run_check(monkeypatch):
    """Count real invocations of autopilot.run_check while still running it."""
    calls = []
    orig = autopilot.run_check

    def counting(cmd, cwd, env, timeout):
        calls.append(cmd)
        return orig(cmd, cwd, env, timeout)

    monkeypatch.setattr(autopilot, "run_check", counting)
    return calls


# -- check cache ---------------------------------------------------------------


def test_check_cache_hit_on_clean_tree(db, worker_ws, monkeypatch):
    calls = count_run_check(monkeypatch)
    env = workspaces.workspace_env(worker_ws)
    ok1, _ = gates.run_checked(db, worker_ws, "true", env, 30)
    ok2, _ = gates.run_checked(db, worker_ws, "true", env, 30)
    assert ok1 and ok2
    assert len(calls) == 1
    assert db.get_check(worker_ws.id, gates.head(worker_ws), "true") is not None


def test_check_cache_miss_on_dirty_tree(db, worker_ws, monkeypatch):
    (Path(worker_ws.path) / "scratch.txt").write_text("uncommitted\n")
    calls = count_run_check(monkeypatch)
    env = workspaces.workspace_env(worker_ws)
    gates.run_checked(db, worker_ws, "true", env, 30)
    gates.run_checked(db, worker_ws, "true", env, 30)
    assert len(calls) == 2
    assert db.get_check(worker_ws.id, gates.head(worker_ws), "true") is None


def test_gates_run_does_not_rerun_checks_at_same_sha(db, worker_ws, monkeypatch):
    calls = count_run_check(monkeypatch)
    cfg = RepoConfig(checks=["true"])
    r1 = gates.run(db, worker_ws, cfg, review_required=False)
    r2 = gates.run(db, worker_ws, cfg, review_required=False)
    assert r1.ok and r2.ok
    assert len(calls) == 1


# -- review prompt ---------------------------------------------------------------


def capture_spawn(monkeypatch):
    captured = {}

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        captured["prompt"] = prompt
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0",
                     None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    return captured


def test_review_prompt_has_task_done_when_and_check_summary(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1", task="Add a login page with OAuth support.",
               done_when="tests/test_login.py passes")
    captured = capture_spawn(monkeypatch)
    cfg = RepoConfig(checks=["true", "false"])
    agents.request_review(db, None, worker_ws, "reviewer", cfg=cfg)
    prompt = captured["prompt"]
    assert "Add a login page with OAuth support." in prompt
    assert "tests/test_login.py passes" in prompt
    assert "PASS `true`" in prompt
    assert "FAIL `false`" in prompt
    assert "Also run:" not in prompt


def test_incremental_review_focuses_on_diff_since_previous_sha(db, worker_ws, monkeypatch):
    sha1 = gates.head(worker_ws)
    db.add_review(worker_ws.id, sha1, "revA", False, "missing input validation on line 10")
    (Path(worker_ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(worker_ws.path))

    captured = capture_spawn(monkeypatch)
    agents.request_review(db, None, worker_ws, "reviewer")
    prompt = captured["prompt"]
    assert sha1[:8] in prompt
    assert "missing input validation on line 10" in prompt
    assert f"git diff {sha1}..HEAD" in prompt


# -- reviewer profile ---------------------------------------------------------------


def test_reviewer_profile_is_cheap():
    p = load_profile("reviewer")
    assert p.model == "sonnet"
    assert p.effort == "medium"
    assert p.strict_mcp is True
    assert p.setting_sources == ["project", "local"]
    assert p.allowed_tools and any("git diff" in t for t in p.allowed_tools)
