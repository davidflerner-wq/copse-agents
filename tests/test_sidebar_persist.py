"""The sidebar coming back after it disappears without the person asking
it to (see agents.sidebar_follow, agents.dismiss_sidebar,
agents._sidebar_lock, tmux.move_pane).

Regression tests for the sidebar going missing and staying gone: it follows
the person into workers' sessions and dies when one of those closes (or
`copse watch` crashes), and before this only a relaunch ever started it
again; a launch that met another process's sidebar lock skipped its sidebar
outright; and a sidebar left alone in its window was never moved out of it.

Real tmux on the suite's private socket (see conftest.py), like
test_sidebar_follow.py.
"""

import fcntl
import sys
import threading
import time

import pytest

from test_sidebar_follow import fake_agent, make_window, make_workspace, window_panes

from copse import agents, cli, tmux
from copse.config import copse_home


@pytest.fixture
def session(tmp_path):
    name = "copse_persisttest"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def wait_dead(pane):
    deadline = time.time() + 5
    while time.time() < deadline and tmux.window_alive(pane):
        time.sleep(0.05)
    assert not tmux.window_alive(pane)


def test_follow_restarts_a_crashed_sidebar_in_the_active_window(db, tmp_path, session):
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    fake_agent(db, ws, win_b, "w1", parent="root1", mode="assign")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    old = db.get_sidebar_pane("root1")

    tmux.kill_pane(old)  # `copse watch` crashed, or its pane was killed
    wait_dead(old)
    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    new = db.get_sidebar_pane("root1")
    assert new not in (None, old, agents.SIDEBAR_DISMISSED)
    assert tmux.window_alive(new)
    assert tmux.get_pane_tag(new, agents.SIDEBAR_TAG) == "root1"
    assert tmux.pane_window(new) == tmux.pane_window(win_b)
    assert len(window_panes(session, "winA")) == 1
    # Beside the agent, at its width, with focus left on the agent.
    assert tmux._tmux("display-message", "-p", "-t", new, "#{pane_width}").stdout.strip() == "30"
    active = tmux._tmux("display-message", "-p", "-t", f"{session}:winB", "#{pane_id}").stdout.strip()
    assert active == win_b
    assert f'-t "{new}"' in tmux._tmux("show-hooks", "-w", "-t", tmux.pane_window(win_b)).stdout


def test_sidebar_killed_with_a_workers_session_comes_back_in_the_root_session(db, tmp_path):
    """The sidebar follows the person into a worker's session; closing that
    session (remove_workspace, a finished worker) takes the sidebar with it.
    Coming back to the supervisor's session must bring it back."""
    root_s, worker_s = "copse_persist_root", "copse_persist_worker"
    tmux.ensure_session(root_s, str(tmp_path), {})
    tmux.ensure_session(worker_s, str(tmp_path), {})
    try:
        root_pane = make_window(root_s, "supervisor")
        worker_pane = make_window(worker_s, "worker")
        root_ws = make_workspace(db, tmp_path, "rootws", root_s)
        worker_ws = make_workspace(db, tmp_path, "workerws", worker_s)
        fake_agent(db, root_ws, root_pane, "root1")
        fake_agent(db, worker_ws, worker_pane, "w1", parent="root1", mode="assign")
        tmux._tmux("select-window", "-t", f"{root_s}:supervisor")
        tmux._tmux("select-window", "-t", f"{worker_s}:worker")
        agents._ensure_sidebar(db, "root1", root_ws, root_pane)
        sidebar = db.get_sidebar_pane("root1")

        agents.sidebar_follow(db, worker_s)  # the person switched to the worker
        assert tmux.pane_window(sidebar) == tmux.pane_window(worker_pane)

        tmux.kill_session(worker_s)
        wait_dead(sidebar)
        agents.sidebar_follow(db, root_s)  # ...and back (or re-attached)

        new = db.get_sidebar_pane("root1")
        assert new != sidebar and tmux.window_alive(new)
        assert tmux.pane_window(new) == tmux.pane_window(root_pane)
    finally:
        tmux.kill_session(root_s)
        if tmux.has_session(worker_s):
            tmux.kill_session(worker_s)


def test_a_dismissed_sidebar_stays_gone_until_a_relaunch(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    agents.dismiss_sidebar(db, sidebar)  # the person pressed q
    tmux.kill_pane(sidebar)
    wait_dead(sidebar)
    assert db.get_sidebar_pane("root1") == agents.SIDEBAR_DISMISSED

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    assert window_panes(session, "winA") == [win_a]

    # `copse` / `copse continue` launches it again.
    agents._ensure_sidebar(db, "root1", ws, win_a)
    new = db.get_sidebar_pane("root1")
    assert new != agents.SIDEBAR_DISMISSED and tmux.window_alive(new)
    assert len(window_panes(session, "winA")) == 2


def test_dismiss_ignores_panes_that_are_not_the_recorded_sidebar(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    agents.dismiss_sidebar(db, None)  # a plain `copse watch` outside tmux
    agents.dismiss_sidebar(db, win_a)  # a plain `copse watch` in some pane
    # An old sidebar pane still tagged for root1 but since replaced.
    stale = make_window(session, "stale")
    tmux.set_pane_tag(stale, agents.SIDEBAR_TAG, "root1")
    agents.dismiss_sidebar(db, stale)

    assert db.get_sidebar_pane("root1") == sidebar


def test_follow_never_starts_one_for_a_root_that_never_had_one(db, tmp_path, session):
    """`copse --no-watch`: no sidebars row at all, so nothing to restore."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    tmux._tmux("select-window", "-t", f"{session}:winA")

    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") is None
    assert window_panes(session, "winA") == [win_a]


def test_relaunch_over_a_row_whose_pane_no_longer_exists(db, tmp_path, session):
    """A tmux server restart (or the pane simply gone) leaves a row naming
    a pane id nothing has: both paths treat it as dead, not as present."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    db.set_sidebar_pane("root1", "%99999")

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    first = db.get_sidebar_pane("root1")
    assert first != "%99999" and tmux.window_alive(first)

    agents._ensure_sidebar(db, "root1", ws, win_a)
    assert db.get_sidebar_pane("root1") == first
    assert len(window_panes(session, "winA")) == 2


def test_follow_moves_a_sidebar_left_alone_in_its_window(db, tmp_path, session):
    """Whatever it sat beside exited (e.g. `exit` in the shell window), so
    the sidebar is the only pane there: switching away must still bring it
    along rather than leave it stranded out of sight."""
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    fake_agent(db, ws, win_b, "w1", parent="root1", mode="assign")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")
    tmux.kill_pane(win_a)
    wait_dead(win_a)

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") == sidebar  # the same process, moved
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_b)
    assert "winA" not in tmux.windows(session)


def _hold_lock(root_id, seconds, held):
    lock = copse_home() / "locks" / f"sidebar-{root_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        held.set()
        time.sleep(seconds)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def test_launch_waits_out_a_brief_lock_instead_of_skipping_its_sidebar(db, tmp_path, session):
    """A sidebar_follow hook holding the lock at the moment of a launch used
    to make _ensure_sidebar give up at once, leaving the session with no
    sidebar (and no row for follow to restore)."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    held = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=("root1", 0.3, held))
    holder.start()
    held.wait(5)
    try:
        agents._ensure_sidebar(db, "root1", ws, win_a)
    finally:
        holder.join()

    assert tmux.window_alive(db.get_sidebar_pane("root1"))
    assert len(window_panes(session, "winA")) == 2


def test_launch_lock_wait_is_bounded(db, tmp_path, session, monkeypatch):
    monkeypatch.setattr(agents, "SIDEBAR_LOCK_TIMEOUT", 0.1)
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    held = threading.Event()
    holder = threading.Thread(target=_hold_lock, args=("root1", 1.0, held))
    holder.start()
    held.wait(5)
    try:
        start = time.monotonic()
        with pytest.raises(BlockingIOError):
            agents._ensure_sidebar(db, "root1", ws, win_a)
        assert time.monotonic() - start < 0.9
    finally:
        holder.join()


def test_quitting_watch_sidebar_dismisses_it(db, tmp_path, session, monkeypatch):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    from copse import watch as watch_mod

    monkeypatch.setattr(watch_mod, "run", lambda repo_root, sidebar=False: None)  # pressed q
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TMUX_PANE", sidebar)
    cli.watch(all_repos=True, once=False, sidebar=True)

    assert db.get_sidebar_pane("root1") == agents.SIDEBAR_DISMISSED


def test_crashing_watch_sidebar_does_not_dismiss_it(db, tmp_path, session, monkeypatch):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    from copse import watch as watch_mod

    def crash(repo_root, sidebar=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(watch_mod, "run", crash)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TMUX_PANE", sidebar)
    with pytest.raises(RuntimeError):
        cli.watch(all_repos=True, once=False, sidebar=True)

    assert db.get_sidebar_pane("root1") == sidebar
