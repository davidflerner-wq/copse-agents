"""Two autopilot reliability bugs: a worker that goes idle without ever
calling report_result must not silently freeze the nudge loop forever, and a
milestone regressed by a merge must not stay hidden behind a stale checkmark
while another milestone is still in progress."""

from __future__ import annotations

import time

import pytest

from copse import agents, autopilot, tmux, workspaces
from copse.db import Agent

CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✶ Thinking… (12s)\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n"
LONG_AGO = time.time() - autopilot.IDLE_GRACE_SECONDS - 1


def add_agent(db, ws, agent_id, *, status="processing", status_since=None, parent="boss",
              provider="claude"):
    a = Agent(agent_id, ws.id, "worker", provider, parent, "assign", status, "@0", None,
              time.time(), status_since=status_since if status_since is not None else time.time())
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def idle_screen(monkeypatch):
    """Workers' terminals show Claude Code's idle input box unless a test says otherwise."""
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)


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
              status_since=LONG_AGO)
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
              status_since=LONG_AGO)
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


def test_codex_worker_never_looks_stalled(db, root, monkeypatch):
    # Codex reports no status through hooks, so its status stays "unknown"
    # however long it has been running; it must keep counting as working.
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "cx", status="unknown", status_since=LONG_AGO, provider="codex")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert autopilot.stalled_workers(db, "boss") == []
    assert [a.id for a in autopilot.working_workers(db, "boss")] == ["cx"]
    assert autopilot.on_stop(db, agent, {}) is None


def test_stale_idle_status_of_a_busy_worker_is_corrected_not_flagged(db, root, monkeypatch):
    # The hooks left "idle" behind, but the worker's screen shows it working.
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle", status_since=LONG_AGO)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BUSY)
    assert autopilot.stalled_workers(db, "boss") == []
    assert db.get_agent("w1").status == "processing"
    assert autopilot.on_stop(db, agent, {}) is None


def test_reconcile_corrects_stale_idle_to_processing(db, root, monkeypatch):
    _, ws = root
    add_agent(db, ws, "w1", status="idle")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BUSY)
    assert agents.reconcile(db, db.get_agent("w1"), gap=0).status == "processing"
    assert db.get_agent("w1").status == "processing"


def test_idle_status_falls_back_to_created_at(db, root, monkeypatch):
    _, ws = root
    a = Agent("w1", ws.id, "worker", "claude", "boss", "assign", "idle", "@0", None, LONG_AGO)
    db.add_agent(a)
    db.conn.execute("UPDATE agents SET status_since=NULL WHERE id='w1'")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert [a.id for a in autopilot.stalled_workers(db, "boss")] == ["w1"]
    db.conn.execute("UPDATE agents SET created_at=? WHERE id='w1'", (time.time(),))
    assert autopilot.stalled_workers(db, "boss") == []


# -- Bug 3: nothing woke a supervisor that went idle before its worker ------


def test_worker_stopping_unreported_wakes_an_idle_supervisor(db, root, monkeypatch):
    # The usual order: the supervisor idles (a worker is still busy), then the
    # worker stops twice without reporting. The supervisor has to hear of it.
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert agents.handle_hook(db, "boss", "stop", {}) is None
    assert db.get_agent("boss").status == "idle"

    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    out = agents.handle_hook(db, "w1", "stop", {})
    assert out and "report_result" in out["reason"]
    assert pasted == []   # reminded once first; nothing to tell yet
    assert agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True}) is None
    assert db.get_agent("w1").status == "idle"
    assert len(pasted) == 1
    assert "w1" in pasted[0] and "report_result" in pasted[0]
    assert ws.branch in pasted[0] and "workspace_diff" in pasted[0]
    assert db.get_agent("boss").status == "processing"


def test_worker_stopping_unreported_is_queued_for_a_busy_supervisor(db, root, monkeypatch):
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True})
    assert pasted == []
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out and out["decision"] == "block"
    assert "w1" in out["reason"] and "stopped without calling report_result" in out["reason"]


def test_synchronous_handoff_does_not_message_its_waiting_caller(db, root):
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    db.update_agent("w1", mode="handoff")
    agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True})
    assert db.pending_count("boss") == 0


# -- Bug 5: milestone rechecks at an unchanged HEAD; regressions aren't progress


def commit(repo, msg="change"):
    import subprocess

    subprocess.run(f"git add -A && git commit -qm {msg}", shell=True, cwd=repo, check=True)


def test_passed_milestone_is_not_rerun_while_head_is_unchanged(db, root, repo, monkeypatch):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    commit(repo)
    autopilot.check_milestones(db, "boss", ws, position=1)
    m1 = db.milestones("boss")[0]
    assert m1.status == "passed" and m1.checked_sha
    ran = []
    real = autopilot.run_check
    monkeypatch.setattr(autopilot, "run_check",
                        lambda cmd, *a: ran.append(cmd) or real(cmd, *a))
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert ran == ["test -f ui.txt"]   # milestone 1 passed at this very commit
    (repo / "ui.txt").write_text("")
    commit(repo)
    ran.clear()
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert ran == ["test -f ui.txt", "test -f api.txt"]   # HEAD moved: recheck


def test_dirty_checkout_always_rechecks(db, root, repo, monkeypatch):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    autopilot.check_milestones(db, "boss", ws, position=1)   # uncommitted
    assert db.milestones("boss")[0].checked_sha is None
    ran = []
    real = autopilot.run_check
    monkeypatch.setattr(autopilot, "run_check",
                        lambda cmd, *a: ran.append(cmd) or real(cmd, *a))
    autopilot.check_milestones(db, "boss", ws, position=2)
    assert "test -f api.txt" in ran


def test_regression_is_not_progress(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    commit(repo)
    autopilot.check_milestones(db, "boss", ws, position=1)
    before = db.get_autopilot("boss").progress
    (repo / "api.txt").unlink()
    commit(repo, "break")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "REGRESSED: milestone 1" in out
    assert db.get_autopilot("boss").progress == before
    # Flaky: it passes again. Newly passing is progress.
    (repo / "api.txt").write_text("")
    commit(repo, "fix")
    autopilot.check_milestones(db, "boss", ws, position=1)
    assert db.get_autopilot("boss").progress == before + 1
