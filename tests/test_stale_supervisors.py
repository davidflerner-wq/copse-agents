"""Supervisor chats whose terminal is gone must not stay in the sidebar
forever. They used to: a supervisor only gets paused by the wrapper around
its own CLI (agents._pause_when_done -> agents.ended), which never runs when
tmux itself goes away (a reboot, `tmux kill-server`), so the row stayed
"idle"; paused sessions were never hidden; and after a tmux restart the new
server hands out the same pane ids (%1 again), so an old supervisor's pane
could even look alive."""

import time

import pytest

from copse import agents, sessions, tmux, view, workspaces
from copse.db import Agent

LONG_AGO = 3600.0


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    yield ws
    tmux.kill_session(ws.tmux_session)


def add(db, ws, agent_id, *, mode="interactive", status="idle", window="", parent=None,
        age=0.0, profile="supervisor"):
    t = time.time() - age
    a = Agent(agent_id, ws.id, profile, "claude", parent, mode, status, window, None, t,
              status_since=t)
    db.add_agent(a)
    return a


def running_window(ws):
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    return tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "300"], {})


def shown(db, repo):
    return {a["id"]: a for ws in view.snapshot(db, str(repo)) for a in ws["agents"]}


def test_a_supervisor_that_died_without_pausing_is_hidden_and_paused(db, repo, root):
    add(db, root, "old", window="%99999", age=LONG_AGO)
    assert "old" not in shown(db, repo)
    # Recorded as agents.pause would have, so it can still be continued.
    assert db.get_agent("old").status == "paused"
    assert [s.root.id for s in sessions.paused(db, str(repo))] == ["old"]
    assert db.get_agent("old").dismissed_at is None


def test_a_paused_supervisor_with_nothing_running_is_hidden_but_resumable(db, repo, root):
    add(db, root, "p1", status="paused", window="%99999", age=LONG_AGO)
    assert "p1" not in shown(db, repo)
    assert agents.latest_paused(db, root).id == "p1"


def test_a_supervisor_that_just_stopped_lingers_briefly(db, repo, root):
    add(db, root, "fresh", window="%99999", age=1)
    assert shown(db, repo)["fresh"]["status"] == "exited"
    assert db.get_agent("fresh").status == "idle"  # not touched yet


def test_a_live_supervisor_and_its_workers_show(db, repo, root):
    pane = running_window(root)
    add(db, root, "boss", window=pane, age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window="%99998", profile="developer",
        age=LONG_AGO)
    assert {"boss", "w1"} <= set(shown(db, repo))


def test_a_dead_supervisor_with_a_live_worker_still_shows(db, repo, root):
    pane = running_window(root)
    add(db, root, "boss", window="%99999", age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window=pane, profile="developer")
    got = shown(db, repo)
    assert got["boss"]["status"] == "exited" and "w1" in got
    assert db.get_agent("boss").status == "idle"


def test_dead_workers_of_a_dead_supervisor_are_paused_with_it(db, repo, root):
    add(db, root, "boss", window="%99999", age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window="%99998", profile="developer",
        age=LONG_AGO)
    view.snapshot(db, str(repo))
    assert db.get_agent("boss").status == "paused"
    assert db.get_agent("w1").status == "paused"
    assert [m.id for m in sessions.paused(db, str(repo))[0].members] == ["boss", "w1"]


def test_a_reused_pane_id_only_counts_for_the_newest_agent(db, repo, root):
    pane = running_window(root)
    add(db, root, "stale", window=pane, age=LONG_AGO)
    add(db, root, "current", window=pane)
    alive = view.live_agents(db, tmux.list_panes())
    assert "current" in alive and "stale" not in alive
    got = shown(db, repo)
    assert "current" in got and "stale" not in got
