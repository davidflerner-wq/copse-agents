"""Closing agents from the sidebar (`x` in `copse watch`, `copse close`):
closed agents are hidden for good and stopped if running, and nothing on
disk is removed."""

import os
import time

import pytest
from typer.testing import CliRunner

from copse import agents, tmux, view, watch, workspaces
from copse.cli import app
from copse.db import Agent


def add_agent(db, ws, agent_id, *, mode="assign", status="idle", window="", parent=None,
              profile="developer"):
    a = Agent(agent_id, ws.id, profile, "claude", parent, mode, status, window, None, time.time())
    db.add_agent(a)
    return a


def running_window(ws):
    """A real (private-socket) tmux pane standing in for an agent's CLI."""
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    return tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "300"], {})


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    yield ws
    tmux.kill_session(ws.tmux_session)


def shown_ids(db, repo_root):
    return [a["id"] for ws in view.snapshot(db, repo_root) for a in ws["agents"]]


def test_dismissed_agents_are_left_out_of_the_snapshot(db, repo, worker_ws):
    add_agent(db, worker_ws, "keep")
    add_agent(db, worker_ws, "gone")
    db.update_agent("gone", dismissed_at=time.time())
    assert shown_ids(db, str(repo)) == ["keep"]
    # `copse ls` still lists everything.
    assert [a["id"] for a in view.workspace_entry(db, worker_ws)["agents"]] == ["keep", "gone"]


def test_a_worktree_whose_agents_are_all_closed_disappears_but_stays_on_disk(db, repo, worker_ws):
    add_agent(db, worker_ws, "w1")
    agents.close(db, "w1")
    assert [ws["id"] for ws in view.snapshot(db, str(repo))] == []
    assert os.path.isdir(worker_ws.path)
    assert db.get_workspace(worker_ws.id) is not None
    assert db.get_agent("w1") is not None


def test_the_main_checkout_stays_even_when_its_agents_are_closed(db, repo):
    root = workspaces.adopt_root(db, str(repo))
    add_agent(db, root, "a1")
    agents.close(db, "a1")
    [entry] = view.snapshot(db, str(repo))
    assert entry["id"] == root.id and entry["agents"] == []


def test_closing_a_running_agent_stops_it(db, worker_ws):
    pane = running_window(worker_ws)
    add_agent(db, worker_ws, "w1", status="processing", window=pane)
    assert agents.is_alive(db.get_agent("w1"))
    agents.close(db, "w1")
    a = db.get_agent("w1")
    assert not agents.is_alive(a)
    assert a.status == "paused" and a.dismissed_at is not None


def test_closing_a_stopped_agent_keeps_its_status(db, worker_ws):
    add_agent(db, worker_ws, "w1", status="done")
    agents.close(db, "w1")
    assert db.get_agent("w1").status == "done"


def test_cli_close_one_agent(db, repo, worker_ws, monkeypatch):
    pane = running_window(worker_ws)
    add_agent(db, worker_ws, "abc123", status="processing", window=pane)
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "abc"])
    assert res.exit_code == 0, res.output
    assert "closed abc123" in res.output and "stopped" in res.output
    assert db.get_agent("abc123").dismissed_at is not None
    assert not agents.is_alive(db.get_agent("abc123"))


def test_cli_close_unknown_agent_fails(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "nope"])
    assert res.exit_code == 1


def test_cli_close_exited_closes_only_stopped_agents(db, repo, worker_ws, monkeypatch):
    pane = running_window(worker_ws)
    add_agent(db, worker_ws, "live", status="processing", window=pane)
    add_agent(db, worker_ws, "dead", status="idle", window="%99999")
    add_agent(db, worker_ws, "paused", status="paused")
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "--exited"])
    assert res.exit_code == 0, res.output
    assert "closed 2 agent(s)" in res.output
    assert db.get_agent("live").dismissed_at is None
    assert agents.is_alive(db.get_agent("live"))
    assert db.get_agent("dead").dismissed_at is not None
    assert db.get_agent("paused").dismissed_at is not None
    assert shown_ids(db, str(repo)) == ["live"]


def test_cli_close_needs_an_id_or_exited(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert CliRunner().invoke(app, ["close"]).exit_code == 1
    assert CliRunner().invoke(app, ["close", "x", "--exited"]).exit_code == 1


def test_resuming_shows_a_closed_agent_again(db, repo, monkeypatch):
    root = workspaces.adopt_root(db, str(repo))
    add_agent(db, root, "boss", mode="interactive", status="paused", profile="supervisor")
    agents.close(db, "boss")
    monkeypatch.setattr(agents, "_launch", lambda *a, **k: None)
    agents.resume(db, "boss")
    assert db.get_agent("boss").dismissed_at is None


# -- the `x` key -----------------------------------------------------------------


def test_x_closes_a_stopped_agent_at_once():
    close, armed, notice = watch.close_request({"id": "a1", "status": "exited"}, None, 100.0)
    assert close and armed is None and "closed" in notice


def test_x_on_a_stopped_row_keeps_another_rows_arming():
    _, armed, _ = watch.close_request({"id": "live", "status": "processing"}, None, 100.0)
    close, still, _ = watch.close_request({"id": "dead", "status": "exited"}, armed, 101.0)
    assert close and still == armed
    close, _, _ = watch.close_request({"id": "live", "status": "processing"}, still, 102.0)
    assert close


def test_x_twice_closes_a_running_agent():
    agent = {"id": "a1", "status": "processing"}
    close, armed, notice = watch.close_request(agent, None, 100.0)
    assert not close and armed == ("a1", 100.0) and "x again" in notice
    close, armed, _ = watch.close_request(agent, armed, 102.0)
    assert close and armed is None


def test_x_confirmation_expires_and_is_per_row():
    agent = {"id": "a1", "status": "idle"}
    _, armed, _ = watch.close_request(agent, None, 100.0)
    close, armed2, _ = watch.close_request(agent, armed, 100.0 + watch.CLOSE_CONFIRM_SECONDS + 1)
    assert not close and armed2 is not None  # too late: re-armed instead
    close, _, _ = watch.close_request({"id": "b2", "status": "idle"}, armed, 101.0)
    assert not close  # a different row needs its own two presses


def test_help_lists_the_close_key():
    assert "x close" in watch.HELP[0]


def test_esc_does_not_quit_the_sidebar():
    assert watch.quit_keys(sidebar=True) == (ord("q"),)
    assert 27 in watch.quit_keys(sidebar=False) and ord("q") in watch.quit_keys(sidebar=False)


def test_watch_sidebar_flag_reaches_the_loop(db, repo, monkeypatch):
    import sys

    seen = {}
    monkeypatch.setattr(watch.curses, "wrapper", lambda fn, root, sidebar: seen.setdefault("sidebar", sidebar))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(agents, "dismiss_sidebar", lambda *a: None)
    monkeypatch.chdir(repo)
    from copse.cli import watch as watch_cmd

    watch_cmd(all_repos=False, once=False, sidebar=True)
    assert seen["sidebar"] is True


# -- pane ids tmux has reused ------------------------------------------------------


def reused_pane(db, ws, *, stale_status="idle", stale_result=None, age=0.0):
    """An old agent ("stale") whose recorded pane id tmux has since given to a
    newer agent ("current"), which is running in it."""
    pane = running_window(ws)
    old = time.time() - age
    db.add_agent(Agent("stale", ws.id, "developer", "claude", None, "assign", stale_status,
                       pane, stale_result, old - 1))
    db.add_agent(Agent("current", ws.id, "developer", "claude", None, "assign", "processing",
                       pane, None, old))
    return pane


def test_only_the_newest_agent_on_a_pane_owns_it(db, worker_ws):
    pane = reused_pane(db, worker_ws)
    assert agents.pane_owners(db) == {pane: "current"}
    assert not agents.owns_pane(db, db.get_agent("stale"))
    assert agents.owns_pane(db, db.get_agent("current"))
    assert view.live_agents(db, tmux.list_panes()) == {"current"}


def test_closing_a_stale_row_never_kills_the_pane_it_used_to_have(db, worker_ws):
    pane = reused_pane(db, worker_ws)
    agents.close(db, "stale")
    assert tmux.window_alive(pane)
    assert db.get_agent("stale").dismissed_at is not None
    assert db.get_agent("stale").status == "idle"  # it wasn't running: nothing stopped
    assert db.get_agent("current").dismissed_at is None


def test_kill_of_a_stale_row_leaves_the_newer_agents_pane(db, worker_ws):
    pane = reused_pane(db, worker_ws)
    agents.kill(db, "stale")
    assert tmux.window_alive(pane) and db.get_agent("stale") is None


def test_killing_the_owner_still_kills_its_pane(db, worker_ws):
    pane = reused_pane(db, worker_ws)
    agents.kill(db, "current")
    assert not tmux.window_alive(pane)


def test_cli_close_of_a_stale_row_says_nothing_was_stopped(db, repo, worker_ws, monkeypatch):
    pane = reused_pane(db, worker_ws)
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "stale"])
    assert res.exit_code == 0, res.output
    assert "closed stale" in res.output and "stopped" not in res.output
    assert tmux.window_alive(pane)


def test_cli_close_exited_skips_a_pane_reused_by_a_live_agent(db, repo, worker_ws, monkeypatch):
    pane = reused_pane(db, worker_ws)
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["close", "--exited"])
    assert res.exit_code == 0, res.output
    assert "closed 1 agent(s)" in res.output
    assert db.get_agent("stale").dismissed_at is not None
    assert db.get_agent("current").dismissed_at is None
    assert tmux.window_alive(pane)


def test_culling_a_stale_worker_leaves_the_newer_agents_pane(db, worker_ws):
    from copse import cull

    # Reported and idle for two hours, past stale_after, on a pane that's now
    # someone else's: closed as stopped, without touching that pane.
    pane = reused_pane(db, worker_ws, stale_result="done", age=7200)
    notes = cull.sweep(db)
    assert any("closed stopped worker stale" in n for n in notes)
    assert db.get_agent("stale").dismissed_at is not None
    assert db.get_agent("current").dismissed_at is None
    assert tmux.window_alive(pane)


def test_quitting_copse_from_the_sidebar_takes_two_presses():
    go, armed, notice = watch.quit_request(None, 100.0)
    assert not go and armed == ("quit", 100.0) and "q again to quit copse" in notice
    go, armed, notice = watch.quit_request(armed, 102.0)
    assert go and armed is None and "paused" in notice
    # Too slow: it arms again instead.
    go, armed, _ = watch.quit_request(("quit", 100.0), 110.0)
    assert not go and armed == ("quit", 110.0)
    # An armed close of some agent doesn't count as a first q.
    go, _, _ = watch.quit_request(("a1b2c3d4", 100.0), 101.0)
    assert not go


def test_sidebar_help_says_q_quits_copse():
    text = "\n".join(ln.text for ln in watch.help_lines(30, in_tmux=True, sidebar=True))
    assert "quit copse" in text
    assert "quit copse" not in "\n".join(ln.text for ln in watch.help_lines(30))


def test_quit_pauses_the_session(db, repo, monkeypatch):
    from typer.testing import CliRunner

    from copse import cli
    from copse.db import Agent

    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0",
                       None, time.time()))
    paused = []
    monkeypatch.setattr(cli.agents, "pause", lambda db, root_id: paused.append(root_id))
    assert CliRunner().invoke(cli.app, ["_quit", "boss"]).exit_code == 0
    assert paused == ["boss"]
