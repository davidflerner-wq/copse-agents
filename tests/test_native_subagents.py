"""Claude Code's own built-in subagents (its Agent tool), surfaced in copse's
sidebar via the SubagentStart/SubagentStop hooks."""

import sqlite3
import time
from dataclasses import replace

import pytest

from copse import agents, tmux, view, watch
from copse.db import DB, Agent
from copse.profiles import load_profile
from copse.providers import ClaudeCode, LaunchContext


def fake_agent(db, ws, status="processing", mode="assign", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def ws(db, repo):
    from copse import workspaces

    return workspaces.create(db, str(repo), "feature").workspace


# -- hooks are registered -----------------------------------------------------


def test_subagent_hooks_are_registered():
    ctx = LaunchContext("abc", load_profile("developer"), "do the thing")
    settings = ClaudeCode().command(ctx)[ClaudeCode().command(ctx).index("--settings") + 1]
    assert "subagent-start" in settings and "SubagentStart" in settings
    assert "subagent-stop" in settings and "SubagentStop" in settings


def test_headless_workers_get_subagent_hooks_too():
    profile = replace(load_profile("developer"), headless=True)
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    assert "-p" in argv
    settings = argv[argv.index("--settings") + 1]
    assert "subagent-start" in settings and "subagent-stop" in settings


# -- hook payloads create/end rows without touching the parent's status ------


def test_start_hook_creates_a_row_without_changing_parent_status(db, ws):
    fake_agent(db, ws, status="processing")
    out = agents.handle_hook(db, "a1", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    assert out is None
    assert db.get_agent("a1").status == "processing"
    [row] = db.native_subagents("a1")
    assert row.id == "sub1" and row.agent_type == "Explore" and row.ended_at is None


def test_stop_hook_ends_the_row_without_changing_parent_status(db, ws):
    fake_agent(db, ws, status="waiting")
    agents.handle_hook(db, "a1", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    out = agents.handle_hook(db, "a1", "subagent-stop",
                             {"agent_id": "sub1", "agent_type": "Explore",
                              "last_assistant_message": "done exploring"})
    assert out is None
    assert db.get_agent("a1").status == "waiting"
    [row] = db.native_subagents("a1")
    assert row.ended_at is not None


def test_missing_agent_id_is_handled_gracefully(db, ws):
    fake_agent(db, ws)
    assert agents.handle_hook(db, "a1", "subagent-start", {"agent_type": "Explore"}) is None
    assert agents.handle_hook(db, "a1", "subagent-stop", {}) is None
    assert db.native_subagents("a1") == []


def test_a_parent_stop_does_not_end_a_still_running_subagent(db, ws):
    # Background subagents keep running after the parent's own turn stops.
    fake_agent(db, ws, mode="assign")
    agents.handle_hook(db, "a1", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True})
    [row] = db.native_subagents("a1")
    assert row.ended_at is None


def test_start_prunes_old_ended_rows_for_the_same_parent(db, ws, monkeypatch):
    fake_agent(db, ws)
    db.start_native_subagent("old", "a1", "Explore")
    db.stop_native_subagent("old")
    later = time.time() + 4000
    monkeypatch.setattr(time, "time", lambda: later)
    db.start_native_subagent("new", "a1", "Plan")
    assert [s.id for s in db.native_subagents("a1")] == ["new"]


def test_start_also_prunes_abandoned_still_running_rows(db, ws, monkeypatch):
    fake_agent(db, ws)
    db.start_native_subagent("crashed", "a1", "Explore")  # never got a SubagentStop
    later = time.time() + 3 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    db.start_native_subagent("new", "a1", "Plan")
    assert [s.id for s in db.native_subagents("a1")] == ["new"]


def test_end_native_subagents_ends_every_running_row_for_a_parent(db, ws):
    fake_agent(db, ws)
    db.start_native_subagent("sub1", "a1", "Explore")
    db.start_native_subagent("sub2", "a1", "Plan")
    db.end_native_subagents("a1")
    assert all(s.ended_at is not None for s in db.native_subagents("a1"))


def test_all_native_subagents_stays_bounded(db, ws, monkeypatch):
    # A stale still-running row and a long-ended row that haven't been
    # pruned yet must not show up in the snapshot query itself.
    fake_agent(db, ws)
    db.start_native_subagent("stale", "a1", "Explore")
    db.start_native_subagent("old_done", "a1", "Plan")
    db.stop_native_subagent("old_done")
    later = time.time() + 3 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    assert db.all_native_subagents() == {}


# -- kept out of agents-table-backed views -------------------------------------


def test_native_subagents_are_invisible_to_agent_listings(db, ws):
    fake_agent(db, ws, agent_id="boss", mode="interactive")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    assert [a.id for a in db.list_agents()] == ["boss"]
    assert db.list_agents(ws.id) and all(a.id != "sub1" for a in db.list_agents(ws.id))


def test_native_subagents_are_invisible_to_autopilot_worker_counts(db, ws):
    from copse import autopilot

    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    db.add_autopilot("boss")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    assert autopilot.active_workers(db, "boss") == []


# -- a stopped parent never shows a subagent as still running -----------------


def test_pausing_the_parent_ends_its_running_subagents(db, ws, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    monkeypatch.setattr(tmux, "kill_session", lambda s: None)
    monkeypatch.setattr(tmux, "windows", lambda s: [])
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})

    agents.pause(db, "boss")
    assert db.get_agent("boss").status == "paused"
    [row] = db.native_subagents("boss")
    assert row.ended_at is not None

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    assert all(s["ended_at"] is not None for s in entry["subagents"])


def test_killing_the_parent_ends_its_running_subagents(db, ws, monkeypatch):
    monkeypatch.setattr(tmux, "kill_window", lambda w: None)
    fake_agent(db, ws, agent_id="boss", mode="assign")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.kill(db, "boss")
    assert db.get_agent("boss") is None  # cascaded away with the parent


def test_agent_entry_drops_running_subs_once_the_parent_is_not_running(db, ws):
    # A parent's process can exit without ever going through agents.pause/kill
    # (e.g. the person closed its terminal), so the view itself must not show
    # a subagent as running once the parent's computed status says otherwise.
    fake_agent(db, ws, agent_id="w1", mode="assign", status="processing")
    agents.handle_hook(db, "w1", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    entry = view.agent_entry(db, db.get_agent("w1"), detail=True)
    assert entry["status"] in view._PARENT_NOT_RUNNING  # no real tmux window: it reads as "exited"
    assert entry["subagents"] == []


# -- sidebar: snapshot and render ---------------------------------------------


def test_snapshot_nests_running_and_recently_done_subagents_under_the_parent(db, ws, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)  # boss is actually running
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub2", "agent_type": "Plan"})
    agents.handle_hook(db, "boss", "subagent-stop", {"agent_id": "sub2", "agent_type": "Plan"})

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    subs = {s["id"]: s for s in entry["subagents"]}
    assert subs["sub1"]["ended_at"] is None
    assert subs["sub2"]["ended_at"] is not None


def test_snapshot_hides_a_stale_running_subagent(db, ws, monkeypatch):
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    later = time.time() + 3 * 3600
    monkeypatch.setattr(time, "time", lambda: later)

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    assert entry["subagents"] == []


def test_snapshot_hides_a_subagent_that_finished_a_while_ago(db, ws, monkeypatch):
    fake_agent(db, ws, agent_id="boss", mode="interactive", status="processing")
    agents.handle_hook(db, "boss", "subagent-start", {"agent_id": "sub1", "agent_type": "Explore"})
    agents.handle_hook(db, "boss", "subagent-stop", {"agent_id": "sub1", "agent_type": "Explore"})
    later = time.time() + 60
    monkeypatch.setattr(time, "time", lambda: later)

    [snap_ws] = view.snapshot(db, ws.repo_root)
    [entry] = [a for a in snap_ws["agents"] if a["id"] == "boss"]
    assert entry["subagents"] == []


def test_render_draws_subagents_one_level_deeper_and_unselectable():
    now = 1000.0
    snap = [{
        "id": "repo/feat", "name": "feat", "branch": "feat", "base_branch": "main",
        "path": "/", "ahead": 0, "behind": 0, "dirty": 0,
        "agents": [{
            "id": "boss", "profile": "supervisor", "provider": "claude", "status": "processing",
            "mode": "interactive", "status_since": now - 30, "pending": 0, "reported": False,
            "window": "@1",
            "subagents": [
                {"id": "sub1", "agent_type": "Explore", "started_at": now - 60, "ended_at": None},
                {"id": "sub2", "agent_type": "Plan", "started_at": now - 90, "ended_at": now - 5},
            ],
        }],
    }]
    lines = watch.render(snap, now=now)
    running = next(ln for ln in lines if "Explore" in ln.text)
    done = next(ln for ln in lines if "Plan" in ln.text)
    assert running.text.strip().startswith("↳ Explore · running")
    assert running.agent is None and running.style == "busy"
    assert done.text.strip() == "↳ Plan · ✓ done"
    assert done.agent is None and done.style == "dim"


# -- migration -----------------------------------------------------------------


def test_migration_adds_the_native_subagents_table_to_an_old_db(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, repo_root TEXT NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, branch TEXT NOT NULL, base_branch TEXT, path TEXT NOT NULL,
          port_base INTEGER, tmux_session TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE agents (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, profile TEXT NOT NULL,
          provider TEXT NOT NULL, parent_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
          tmux_window TEXT NOT NULL, result TEXT, created_at REAL NOT NULL);
        INSERT INTO workspaces VALUES ('w','/r','n','main','b',NULL,'/p',NULL,'s',0);
        INSERT INTO agents VALUES ('a','w','p','claude',NULL,'assign','idle','@1',NULL,7.0);
    """)
    con.commit()
    con.close()

    db = DB(str(path))
    db.start_native_subagent("sub1", "a", "Explore")
    [row] = db.native_subagents("a")
    assert row.agent_type == "Explore"
