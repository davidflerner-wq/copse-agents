"""merge_workspace's pre-gate sync: bring a stale branch's local base in
before checks run, and turn conflicts and merge failures into readable
"Not merged: ..." replies instead of raising."""

import asyncio
import json
import os
import time

import pytest

from conftest import sh
from copse import agents, git, mcp_server, tmux, workspaces
from copse.db import Agent


def write_config(repo, **cfg):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A supervisor caller adopted on the main checkout, so mcp_server tools
    that need `_caller` resolve without touching the real process cwd."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


def _advance_base(repo, filename="base.txt", content="b"):
    """Add a commit to the local base branch, without touching origin."""
    (repo / filename).write_text(content)
    sh(f"git add -A && git commit -qm {filename}", repo)


# -- workspaces.sync_with_base -----------------------------------------------


def test_sync_with_base_merges_a_behind_branch(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    before = git.out(["rev-parse", "HEAD"], ws.path)
    _advance_base(repo)

    result = workspaces.sync_with_base(ws)

    assert result.status == "synced"
    assert result.new_sha and result.new_sha != before
    assert os.path.exists(os.path.join(ws.path, "base.txt"))
    assert git.dirty_files(ws.path) == []


def test_sync_with_base_conflict_aborts_and_stays_clean(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "w").write("mine\n")
    git.commit_all(ws.path, "mine")
    before = git.out(["rev-parse", "HEAD"], ws.path)
    (repo / "app.py").write_text("theirs\n")
    sh("git add -A && git commit -qm theirs", repo)

    result = workspaces.sync_with_base(ws)

    assert result.status == "conflict"
    assert result.conflicts == ["app.py"]
    # Left exactly as it was: clean, no merge in progress, same HEAD.
    assert git.dirty_files(ws.path) == []
    assert not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)
    assert git.out(["rev-parse", "HEAD"], ws.path) == before


def test_sync_with_base_up_to_date_is_a_noop(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")

    result = workspaces.sync_with_base(ws)

    assert result.status == "up_to_date"
    assert result.new_sha is None


def test_sync_with_base_raises_on_non_conflict_merge_failure(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    before = git.out(["rev-parse", "HEAD"], ws.path)
    _advance_base(repo)
    sh("git config merge.ff only", ws.path)  # diverged branches can't fast-forward

    with pytest.raises(git.GitError, match="merging main failed"):
        workspaces.sync_with_base(ws)

    # Aborted cleanly, not mistaken for a conflict or a successful sync.
    assert git.dirty_files(ws.path) == []
    assert not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)
    assert git.out(["rev-parse", "HEAD"], ws.path) == before


def test_sync_with_base_skips_a_dirty_worktree(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    _advance_base(repo)
    open(os.path.join(ws.path, "scratch.txt"), "w").write("wip")  # uncommitted

    result = workspaces.sync_with_base(ws)

    assert result.status == "skipped"
    assert not os.path.exists(os.path.join(ws.path, "base.txt"))


# -- merge_workspace tool -----------------------------------------------------


def test_merge_workspace_syncs_then_asks_for_a_new_review(db, repo, boss):
    write_config(repo, review=True)
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert "Not merged: synced" in out
    assert "feature" in out and "main" in out
    assert "request_review" in out
    # The branch itself was brought up to date even though nothing merged.
    assert os.path.exists(os.path.join(ws.path, "base.txt"))
    assert git.dirty_files(ws.path) == []
    assert not (repo / "f.txt").exists()  # main untouched


def test_merge_workspace_reports_non_conflict_sync_failure(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)
    sh("git config merge.ff only", ws.path)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Not merged: merging main failed")
    assert git.dirty_files(ws.path) == []
    assert not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)
    assert not (repo / "f.txt").exists()  # main untouched


CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"


def _stale_branch_with_worker(db, repo, mode="assign", result=None):
    """A branch behind main, with worker w1 marked processing in it."""
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    _advance_base(repo)
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", mode, "processing",
                        "@1", result, time.time()))
    return ws


@pytest.fixture
def live_worker(monkeypatch):
    """Workers' windows count as alive, with a screen reconcile can't read
    (so the hook status stands)."""
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")


@pytest.mark.parametrize("mode", ["assign", "handoff", "handoff_detached"])
def test_merge_workspace_skips_sync_when_worker_is_busy(db, repo, boss, live_worker, mode):
    ws = _stale_branch_with_worker(db, repo, mode)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out == "Not merged: w1 is still working on feature; retry once it reports."
    assert not os.path.exists(os.path.join(ws.path, "base.txt"))  # sync never ran


def test_merge_workspace_ignores_busy_worker_when_no_sync_needed(db, repo, boss, live_worker):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                        "@1", None, time.time()))

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")


def test_merge_workspace_ignores_worker_whose_window_is_dead(db, repo, boss):
    ws = _stale_branch_with_worker(db, repo)  # "@1" isn't a live window

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert (repo / "f.txt").exists()


def test_merge_workspace_reconciles_a_stale_processing_worker(db, repo, boss, monkeypatch):
    ws = _stale_branch_with_worker(db, repo)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert db.get_agent("w1").status == "idle"


def test_merge_workspace_ignores_worker_that_already_reported(db, repo, boss, live_worker):
    # report_result was called; its Stop hook just hasn't fired yet.
    ws = _stale_branch_with_worker(db, repo, "handoff", result="done")

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")


def test_merge_workspace_does_not_block_a_worker_merging_its_own_branch(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "processing",
                        "@1", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "w1")

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")


def test_merge_workspace_conflict_reports_files_and_stays_clean(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "w").write("mine\n")
    git.commit_all(ws.path, "mine")
    (repo / "app.py").write_text("theirs\n")
    sh("git add -A && git commit -qm theirs", repo)

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out == (
        "Not merged: feature conflicts with main in: app.py. "
        "Ask the worker to merge main and resolve."
    )
    assert git.dirty_files(ws.path) == []
    assert not git.ok(["rev-parse", "-q", "--verify", "MERGE_HEAD"], ws.path)


def test_merge_workspace_up_to_date_branch_merges_as_before(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert (repo / "new.py").read_text() == "x = 1\n"


def test_merge_into_ignores_untracked_files_in_target(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    (repo / ".DS_Store").write_bytes(b"junk")  # untracked cruft in the target checkout

    target = workspaces.merge_back(db, ws)

    assert target == str(repo)
    assert (repo / "new.py").read_text() == "x = 1\n"
    assert (repo / ".DS_Store").read_bytes() == b"junk"  # left alone


def test_merge_workspace_succeeds_despite_untracked_files_in_target(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")
    (repo / ".DS_Store").write_bytes(b"junk")

    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out.startswith("Merged feature into main")
    assert (repo / "new.py").read_text() == "x = 1\n"


def test_merge_workspace_reports_giterror_from_merge_back(db, repo, boss, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")

    def boom(*a, **k):
        raise git.GitError("boom")

    monkeypatch.setattr(workspaces, "merge_back", boom)
    out = asyncio.run(mcp_server.merge_workspace(ws.id))

    assert out == "Not merged: boom"
