"""The one sidebar pane following the user between copse windows and
sessions (see agents.sidebar_follow, tmux.set_follow_hooks, tmux.move_pane).

Uses real tmux on the private socket the test suite already runs on
(COPSE_TMUX_SOCKET, see conftest.py's private_tmux_server fixture), since
the behaviour under test is genuine tmux pane/hook plumbing.
"""

import time

import pytest

from copse import agents, tmux
from copse.db import DB, Workspace


@pytest.fixture
def db(copse_home):
    return DB()


def make_workspace(db, tmp_path, name, session, repo_root=None):
    path = tmp_path / name
    path.mkdir()
    ws = Workspace(
        id=name, repo_root=repo_root or str(tmp_path), name=name, kind="worktree",
        branch=name, base_branch="main", path=str(path), port_base=None,
        tmux_session=session, created_at=time.time(),
    )
    db.add_workspace(ws)
    return ws


def window_panes(session, window):
    return tmux._tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}").stdout.split()


def make_window(session, name):
    """A window with one plain shell pane, standing in for an agent's pane."""
    proc = tmux._tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:", "-n", name)
    return proc.stdout.strip()


@pytest.fixture
def session(tmp_path):
    name = "copse_followtest"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_apply_theme_sets_follow_hooks_on_the_session_only(session):
    tmux.apply_theme(session)
    out = tmux._tmux("show-hooks", "-t", session).stdout
    assert "session-window-changed" in out
    assert "client-session-changed" in out
    assert "_sidebar-follow" in out
    assert session in out  # the session name is baked into the command itself
    assert "_sidebar-follow" not in tmux._tmux("show-hooks", "-g").stdout


def test_plain_tmux_session_is_never_hooked():
    tmux._tmux("new-session", "-d", "-s", "not_a_copse_session")
    try:
        out = tmux._tmux("show-hooks", "-t", "not_a_copse_session").stdout
        assert "_sidebar-follow" not in out
    finally:
        tmux.kill_session("not_a_copse_session")


def test_sidebar_follow_creates_it_beside_the_active_window(db, tmp_path, session):
    win_a = make_window(session, "winA")
    tmux._tmux("select-window", "-t", f"{session}:winA")

    agents.sidebar_follow(db, "no-such-session")  # unknown session: no crash
    make_workspace(db, tmp_path, "winA", session)

    agents.sidebar_follow(db, session)
    sidebar = db.get_sidebar_pane(str(tmp_path))
    assert sidebar is not None
    assert len(window_panes(session, "winA")) == 2
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_a)


def test_sidebar_follow_moves_the_pane_to_the_new_active_window(db, tmp_path, session):
    make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    sidebar = db.get_sidebar_pane(ws.repo_root)
    assert len(window_panes(session, "winA")) == 2
    assert len(window_panes(session, "winB")) == 1

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane(ws.repo_root) == sidebar  # same pane, just relocated
    assert len(window_panes(session, "winA")) == 1
    assert len(window_panes(session, "winB")) == 2
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_b)
    # winB's active pane is still its own agent pane, not the sidebar
    active = tmux._tmux("display-message", "-p", "-t", f"{session}:winB", "#{pane_id}").stdout.strip()
    assert active == win_b


def test_sidebar_follow_is_a_noop_in_the_window_it_already_holds(db, tmp_path, session):
    make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    sidebar = db.get_sidebar_pane(ws.repo_root)
    width_before = tmux._tmux(
        "display-message", "-p", "-t", sidebar, "#{pane_width}"
    ).stdout.strip()

    # Calling it again for the same window must not re-join the pane onto
    # itself (join-pane isn't idempotent: doing that scrambles the layout).
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane(ws.repo_root) == sidebar
    assert len(window_panes(session, "winA")) == 2
    width_after = tmux._tmux(
        "display-message", "-p", "-t", sidebar, "#{pane_width}"
    ).stdout.strip()
    assert width_after == width_before == "30"


def test_window_resized_hook_moves_with_the_sidebar(db, tmp_path, session):
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    sidebar = db.get_sidebar_pane(ws.repo_root)

    win_a_id = tmux.pane_window(win_a)
    win_b_id = tmux.pane_window(win_b)
    assert f'-t "{sidebar}"' in tmux._tmux("show-hooks", "-w", "-t", win_a_id).stdout

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert "window-resized" not in tmux._tmux("show-hooks", "-w", "-t", win_a_id, check=False).stdout
    assert f'-t "{sidebar}"' in tmux._tmux("show-hooks", "-w", "-t", win_b_id).stdout


def test_sidebar_follow_recreates_a_dead_sidebar(db, tmp_path, session):
    make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    old_sidebar = db.get_sidebar_pane(ws.repo_root)

    tmux.kill_pane(old_sidebar)  # simulate a crash
    deadline = time.time() + 5
    while time.time() < deadline and tmux.window_alive(old_sidebar):
        time.sleep(0.1)

    agents.sidebar_follow(db, session)

    new_sidebar = db.get_sidebar_pane(ws.repo_root)
    assert new_sidebar is not None and new_sidebar != old_sidebar
    assert tmux.window_alive(new_sidebar)
    assert len(window_panes(session, "winA")) == 2


def test_plain_shell_window_gets_the_sidebar_too(db, tmp_path):
    """ensure_session's own first window (named 'shell') is a normal target."""
    name = "copse_followtest_shell"
    tmux.ensure_session(name, str(tmp_path), {})
    try:
        ws = make_workspace(db, tmp_path, "shellws", name)
        tmux._tmux("select-window", "-t", f"{name}:shell")
        agents.sidebar_follow(db, name)
        sidebar = db.get_sidebar_pane(ws.repo_root)
        assert sidebar is not None
        assert len(window_panes(name, "shell")) == 2
    finally:
        tmux.kill_session(name)


def test_pause_cleans_up_the_sidebar_wherever_it_is(db, tmp_path, session, monkeypatch):
    from copse.db import Agent

    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    sidebar = db.get_sidebar_pane(ws.repo_root)
    assert tmux.window_alive(sidebar)

    agent = Agent("root1", ws.id, "supervisor", "claude", None, "interactive",
                  "idle", win_a, None, time.time())
    db.add_agent(agent)

    agents.pause(db, "root1")

    assert not tmux.window_alive(sidebar)
    assert db.get_sidebar_pane(ws.repo_root) is None
