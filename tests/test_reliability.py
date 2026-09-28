"""Two autopilot reliability bugs: a worker that goes idle without ever
calling report_result must not silently freeze the nudge loop forever, and a
milestone regressed by a merge must not stay hidden behind a stale checkmark
while another milestone is still in progress."""

from __future__ import annotations

import time

import pytest

from copse import agents, autopilot, tmux, workspaces
from copse.db import Agent
from test_agents import CLAUDE_BUSY_REAL_CAPTURE

CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
# Real Claude Code 2.1.283 layout: a blank line sits between the box's top
# border and the spinner line above it (and another below the bottom
# border), e.g. "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)".
BOX = "\n────\n❯ \n────\n\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)" + BOX
CLAUDE_DONE = "✻ Sautéed for 7m 49s · done 7:57 PM" + BOX
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
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert autopilot.split_workers(db, "boss", screen=True)[1] == []
    assert db.get_agent("w1").status == "processing"
    assert autopilot.on_stop(db, agent, {}) is None


def test_stale_idle_status_is_corrected_against_a_real_captured_busy_screen(db, root, monkeypatch):
    # An exact capture of a real Claude Code 2.1.283 pane (shared with
    # test_agents.py): a blank line above and below the box, a truncated
    # transcript line further up, and a spinner directly above the top
    # blank line.
    agent, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle", status_since=LONG_AGO)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BUSY_REAL_CAPTURE)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert autopilot.split_workers(db, "boss", screen=True)[1] == []
    assert db.get_agent("w1").status == "processing"


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
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BUSY)
    monkeypatch.setattr(time, "sleep", lambda s: None)
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


def test_flaky_pass_at_the_same_commit_is_not_progress(db, root, repo):
    _, ws = root
    with_goal(db, checks=("test -f api.txt", "test -f ui.txt"))
    (repo / "api.txt").write_text("")
    commit(repo)
    autopilot.check_milestones(db, "boss", ws, position=1)
    before = db.get_autopilot("boss").progress
    m1 = db.milestones("boss")[0]
    db.record_check(m1.id, False, "flaked", m1.checked_sha)   # failed once, same HEAD
    assert db.milestones("boss")[0].passed_sha == m1.checked_sha
    autopilot.check_milestones(db, "boss", ws, position=1)
    assert db.milestones("boss")[0].status == "passed"
    assert db.get_autopilot("boss").progress == before


# -- Re-review: a cheap dashboard, no false busy, parents always told -------


def test_dashboard_never_sleeps_or_reads_idle_screens(db, root, monkeypatch):
    from copse import view

    _, ws = root
    with_goal(db)
    add_agent(db, ws, "w1", status="idle", status_since=LONG_AGO)
    add_agent(db, ws, "w2", status="idle")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)

    def no_sleep(s):
        raise AssertionError("the dashboard slept")

    captured = []
    monkeypatch.setattr(time, "sleep", no_sleep)
    monkeypatch.setattr(tmux, "capture", lambda target, **k: captured.append(target) or CLAUDE_BUSY)
    monkeypatch.setattr(tmux, "window_alive", lambda w: True)
    db.set_status("boss", "idle")   # every agent idle: nothing to read at all
    entry = view.autopilot_entry(db, ws.repo_root)
    assert entry and entry["workers"] == 1   # w2; w1 is stalled
    view.snapshot(db, ws.repo_root)
    assert captured == []
    assert db.get_agent("w1").status == "idle"
    assert db.get_agent("w2").status == "idle"


def test_idle_screen_quoting_the_busy_marker_stays_idle(db, root, monkeypatch):
    # The spinner text appears in the transcript, scrolled well above the
    # input box, not in the few lines right by it where a real spinner runs.
    _, ws = root
    add_agent(db, ws, "w1", status="idle")
    quoted = (
        "⏺ The status line says \"Tomfoolering… (7m 22s · ↓ 35.0k tokens)\" while a turn runs.\n"
        "\n"
        "That was earlier in the conversation.\n"
        "\n"
        "Then some more output happened here.\n"
        "\n"
        "────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ? for shortcuts\n"
    )
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: quoted)
    assert agents.reconcile(db, db.get_agent("w1"), gap=0).status == "idle"
    assert db.get_agent("w1").status == "idle"


def test_done_spinner_layout_is_recognized_as_idle(db, root, monkeypatch):
    _, ws = root
    add_agent(db, ws, "w1", status="waiting")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_DONE)
    assert agents.reconcile(db, db.get_agent("w1"), gap=0).status == "idle"
    assert db.get_agent("w1").status == "idle"


def test_reconciled_to_idle_delivers_queued_message(db, root, monkeypatch):
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    db.enqueue("w1", "hello", "boss")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    agents.reconcile(db, db.get_agent("w1"), gap=0)
    assert pasted == ["hello"]
    assert db.pending_count("w1") == 0


def test_single_sample_reconcile_never_flushes_a_queued_message(db, root, monkeypatch):
    # The dashboard reads with samples=1 (view.agent_entry): it may correct a
    # stale status, but must never paste a queued message as a side effect of
    # just being rendered.
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    db.enqueue("w1", "hello", "boss")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    agents.reconcile(db, db.get_agent("w1"), samples=1, gap=0)
    assert db.get_agent("w1").status == "idle"
    assert pasted == []
    assert db.pending_count("w1") == 1


def test_send_message_delivered_when_reconcile_already_flushed_it(db, root, monkeypatch):
    # The agent is "processing" in the DB but its screen already shows idle;
    # reconcile's own idle correction flushes the message before send_message
    # gets a chance to call flush itself, which must not read back "queued".
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    result = agents.send_message(db, "w1", "hello", sender_id="boss")
    assert result == "delivered"
    assert len(pasted) == 1
    assert db.pending_count("w1") == 0


def test_send_message_reports_queued_when_an_older_message_is_flushed_instead(db, root, monkeypatch):
    # An older message was already queued. reconcile's idle correction flushes
    # that one (the oldest), not the one send_message just enqueued, so this
    # must report "queued" for it rather than mistaking the older delivery
    # for its own.
    _, ws = root
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    db.enqueue("w1", "older", "boss")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    result = agents.send_message(db, "w1", "newer", sender_id="boss")
    assert pasted == ["older"]
    assert result == "queued"
    assert db.pending_count("w1") == 1


def test_worker_stopping_unreported_tells_a_hookless_parent(db, root, monkeypatch):
    # A Codex supervisor has no Stop hook to hand over a queued message, so
    # it has to be typed in straight away.
    agent, ws = root
    db.update_agent("boss", provider="codex", status="unknown")
    add_agent(db, ws, "w1", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, text: pasted.append(text))
    agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True})
    assert len(pasted) == 1
    assert "w1" in pasted[0] and "won't be reminded again on its own" in pasted[0]
    assert db.pending_count("boss") == 0
