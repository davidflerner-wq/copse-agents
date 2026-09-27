"""Two autopilot reliability bugs: a worker that goes idle without ever
calling report_result must not silently freeze the nudge loop forever, and a
milestone regressed by a merge must not stay hidden behind a stale checkmark
while another milestone is still in progress."""

from __future__ import annotations

import time

import pytest

from copse import agents, autopilot, workspaces
from copse.db import Agent


def add_agent(db, ws, agent_id, *, status="processing", status_since=None, parent="boss"):
    a = Agent(agent_id, ws.id, "worker", "claude", parent, "assign", status, "@0", None,
              time.time(), status_since=status_since if status_since is not None else time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    supervisor = Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                       "processing", "@0", None, time.time())
    db.add_agent(supervisor)
    db.add_autopilot("boss")
    return supervisor, ws


def with_goal(db, checks=("test -f api.txt", "test -f ui.txt")):
    autopilot.set_goal(db, "boss", "Settings page",
                       [(f"Milestone {i}", c, None) for i, c in enumerate(checks, start=1)])


# -- Bug 1: an idle, unreported worker must not stall the nudge loop --------


def test_worker_idle_past_grace_period_does_not_block_the_nudge(db, root, monkeypatch):
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle",
              status_since=time.time() - autopilot.IDLE_GRACE_SECONDS - 1)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out = autopilot.on_stop(db, agent, {})
    assert out and out["decision"] == "block"
    assert "w1" in out["reason"]
    assert "report_result" in out["reason"]


def test_busy_worker_still_defers(db, root, monkeypatch):
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert autopilot.on_stop(db, agent, {}) is None


def test_worker_idle_within_grace_period_still_defers(db, root, monkeypatch):
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle", status_since=time.time() - 5)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert autopilot.on_stop(db, agent, {}) is None


def test_stalled_worker_is_still_active_but_not_working(db, root, monkeypatch):
    _, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle",
              status_since=time.time() - autopilot.IDLE_GRACE_SECONDS - 1)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    # Still counted as open work (e.g. for the max_agents cap, and so its
    # workspace isn't forgotten), but no longer "working" on its own.
    assert [a.id for a in autopilot.active_workers(db, "boss")] == ["w1"]
    assert [a.id for a in autopilot.stalled_workers(db, "boss")] == ["w1"]
    assert autopilot.working_workers(db, "boss") == []


# -- Bug 2: a milestone regression must not hide behind a stale checkmark ---


def test_checking_one_milestone_rechecks_a_passed_one_and_reports_regression(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=1)
    assert "1 of 2" in out and "REGRESSED" not in out
    (repo / "api.txt").unlink()   # milestone 1 regresses while milestone 2 is still pending
    (repo / "ui.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "REGRESSED: milestone 1" in out
    assert [m.status for m in db.milestones("boss")] == ["failed", "passed"]


def test_checking_a_milestone_with_nothing_regressed_stays_quiet(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    autopilot.check_milestones(db, "boss", ws, position=1)
    (repo / "ui.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "REGRESSED" not in out
    assert "goal is reached" in out
