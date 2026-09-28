"""merge_workspace's pre-gate sync: bring a stale branch's local base in
before checks run, and turn conflicts and merge failures into readable
"Not merged: ..." replies instead of raising."""

import asyncio
import json
import os
import time

import pytest

from conftest import sh
from copse import git, mcp_server, workspaces
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
    assert not os.path.exists(os.path.join(ws.path, ".git", "MERGE_HEAD"))
    assert git.out(["rev-parse", "HEAD"], ws.path) == before


def test_sync_with_base_up_to_date_is_a_noop(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")

    result = workspaces.sync_with_base(ws)

    assert result.status == "up_to_date"
    assert result.new_sha is None


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
    assert not os.path.exists(os.path.join(ws.path, ".git", "MERGE_HEAD"))


def test_merge_workspace_up_to_date_branch_merges_as_before(db, repo, boss):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    git.commit_all(ws.path, "work")

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
