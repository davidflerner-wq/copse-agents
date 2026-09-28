import os
import shutil
import time

import pytest

from copse import agents, git, status_cache, tmux, view, workspaces
from copse.db import DB

from conftest import sh


def make_workspaces(db, repo, names):
    return [workspaces.create(db, str(repo), name).workspace for name in names]


def count_calls(monkeypatch, target, name):
    calls = []
    orig = getattr(target, name)

    def counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(target, name, counting)
    return calls


# -- fingerprint cache: warm snapshots and per-workspace invalidation -------


def test_warm_snapshot_makes_zero_git_subprocesses(db, repo, monkeypatch):
    ws = make_workspaces(db, repo, ["one"])[0]
    view.snapshot(db, str(repo))  # cold: populates the cache

    calls = count_calls(monkeypatch, git, "run")
    view.snapshot(db, str(repo))
    assert calls == []


def test_file_edit_invalidates_only_that_workspace(db, repo, monkeypatch):
    ws1, ws2 = make_workspaces(db, repo, ["one", "two"])
    view.snapshot(db, str(repo))  # cold for both

    with open(os.path.join(ws1.path, "new.txt"), "w") as f:
        f.write("x")

    calls = count_calls(monkeypatch, git, "run")
    view.snapshot(db, str(repo))
    touched = {str(c[1]) for c in calls}
    assert any(ws1.path in t for t in touched)
    assert not any(ws2.path in t for t in touched)


def test_commit_invalidates_only_that_workspace(db, repo, monkeypatch):
    ws1, ws2 = make_workspaces(db, repo, ["one", "two"])
    view.snapshot(db, str(repo))  # cold for both

    with open(os.path.join(ws2.path, "b.txt"), "w") as f:
        f.write("b")
    git.commit_all(ws2.path, "wip")

    calls = count_calls(monkeypatch, git, "run")
    view.snapshot(db, str(repo))
    touched = {str(c[1]) for c in calls}
    assert any(ws2.path in t for t in touched)
    assert not any(ws1.path in t for t in touched)


def test_safety_net_forces_recompute_after_ttl(db, repo):
    ws = make_workspaces(db, repo, ["one"])[0]
    status_cache.cached_status(ws.path, "main", now=0.0)

    # Editing an already-tracked file in place touches neither the index nor
    # the worktree root's mtime, so the fingerprint alone won't see it.
    with open(os.path.join(ws.path, "app.py"), "a") as f:
        f.write("more\n")

    stale = status_cache.cached_status(ws.path, "main", now=5.0)
    assert stale.dirty_files == []

    fresh = status_cache.cached_status(ws.path, "main", now=status_cache.TTL + 1.0)
    assert "app.py" in fresh.dirty_files


# -- parity with the old git.status for several scenarios -------------------


def test_status_matches_old_status_when_dirty_and_untracked(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]
    with open(os.path.join(ws.path, "app.py"), "a") as f:
        f.write("print(2)\n")
    with open(os.path.join(ws.path, "new.txt"), "w") as f:
        f.write("x")

    old = git.status(ws.path, "main")
    new = status_cache.cached_status(ws.path, "main")
    assert (new.branch, new.ahead, new.behind, sorted(new.dirty_files), new.unpushed) == \
           (old.branch, old.ahead, old.behind, sorted(old.dirty_files), old.unpushed)


def test_status_matches_old_status_when_ahead(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]
    with open(os.path.join(ws.path, "f.txt"), "w") as f:
        f.write("f")
    git.commit_all(ws.path, "feature work")

    old = git.status(ws.path, "main")
    status_cache.clear()
    new = status_cache.cached_status(ws.path, "main")
    assert (new.ahead, new.behind, new.unpushed) == (old.ahead, old.behind, old.unpushed)


def test_status_matches_old_status_when_behind(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]

    other = repo.parent / "other"
    sh(f"git clone -q {repo.parent / 'origin.git'} {other}", repo.parent)
    (other / "b.txt").write_text("b")
    sh("git add -A && git commit -qm upstream && git push -q", other)
    sh("git fetch -q origin main", ws.path)

    old = git.status(ws.path, "main")
    status_cache.clear()
    new = status_cache.cached_status(ws.path, "main")
    assert (new.ahead, new.behind) == (old.ahead, old.behind) == (0, 1)


def test_status_matches_old_status_for_missing_base(db, repo):
    ws = make_workspaces(db, repo, ["feature"])[0]

    with pytest.raises(git.GitError):
        git.status(ws.path, "ghost")
    status_cache.clear()
    with pytest.raises(git.GitError):
        status_cache.cached_status(ws.path, "ghost")


# -- tmux: one list-panes per snapshot, shared across agents -----------------


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_is_alive_uses_one_list_panes_per_snapshot(db, repo, monkeypatch):
    ws = make_workspaces(db, repo, ["one"])[0]
    a1 = agents.spawn(db, ws, "developer", provider_name="shell")
    a2 = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not (agents.is_alive(a1) and agents.is_alive(a2)):
            time.sleep(0.1)
        assert agents.is_alive(a1) and agents.is_alive(a2)

        calls = count_calls(monkeypatch, tmux, "_tmux")
        view.snapshot(db, str(repo))
        list_panes_calls = [c for c in calls if c and c[0] == "list-panes"]
        display_message_calls = [c for c in calls if c and c[0] == "display-message"]
        assert len(list_panes_calls) == 1
        assert display_message_calls == []
    finally:
        tmux.kill_session(ws.tmux_session)


# -- benchmark: subprocess counts for 3 workspaces x 2 agents ----------------


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_snapshot_subprocess_counts_cold_and_warm(db, repo, monkeypatch, capsys):
    wss = make_workspaces(db, repo, ["one", "two", "three"])
    spawned = []
    try:
        for ws in wss:
            for _ in range(2):
                spawned.append(agents.spawn(db, ws, "developer", provider_name="shell"))

        deadline = time.time() + 5
        while time.time() < deadline and not all(agents.is_alive(a) for a in spawned):
            time.sleep(0.1)
        assert all(agents.is_alive(a) for a in spawned)

        git_calls = count_calls(monkeypatch, git, "run")
        tmux_calls = count_calls(monkeypatch, tmux, "_tmux")
        view.snapshot(db, str(repo))
        cold_git, cold_tmux = len(git_calls), len(tmux_calls)

        git_calls.clear()
        tmux_calls.clear()
        view.snapshot(db, str(repo))
        warm_git, warm_tmux = len(git_calls), len(tmux_calls)

        print(f"\ndashboard perf: cold git={cold_git} tmux={cold_tmux}; "
              f"warm git={warm_git} tmux={warm_tmux}")

        assert warm_git == 0
        assert cold_git > warm_git
        assert warm_tmux == 1  # one shared list-panes call per snapshot, even warm
    finally:
        for ws in wss:
            tmux.kill_session(ws.tmux_session)
