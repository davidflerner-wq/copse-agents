import json
import shutil

import pytest

from copse import git, pool, sessions, view, workspaces
from copse.config import load_repo_config

from conftest import sh


def write_config(repo, **cfg):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps(cfg))


@pytest.fixture(autouse=True)
def no_background_fill(monkeypatch):
    # create()/start() launch a detached `copse _pool-fill` subprocess after a
    # claim; that would race these tests' own counter-file assertions since it
    # runs against the same test DB. Refilling itself is covered directly via
    # pool.fill()/fill_locked().
    monkeypatch.setattr(pool, "fill_in_background", lambda repo_root: None)


def test_fill_creates_entry_and_runs_setup_once(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    cfg = load_repo_config(str(repo))
    assert cfg.pool_size == 1  # default: repo has setup commands

    added = pool.fill(db, str(repo))
    assert added == 1
    assert counter.read_text().count("x") == 1
    entries = db.pool_entries(str(repo), "main")
    assert len(entries) == 1
    assert entries[0].base_branch == "main"

    # Idempotent: pool is already at pool_size.
    assert pool.fill(db, str(repo)) == 0


def test_claim_reuses_without_rerunning_setup(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    pool.fill(db, str(repo))
    assert counter.read_text().count("x") == 1

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert created.setup is None
    assert counter.read_text().count("x") == 1
    assert db.count_pool_entries(str(repo), "main") == 0


def test_lockfile_change_triggers_setup_again(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    (repo / "uv.lock").write_text("v1\n")
    sh("git add -A && git commit -qm lock", repo)
    pool.fill(db, str(repo))
    assert counter.read_text().count("x") == 1

    (repo / "uv.lock").write_text("v2\n")
    sh("git add -A && git commit -qm lock2", repo)

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert created.setup is not None and created.setup.ok
    assert counter.read_text().count("x") == 2
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == git.out(
        ["rev-parse", "main"], str(repo)
    )


def test_claim_lands_at_current_base_when_it_moved_forward(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    pool.fill(db, str(repo))
    assert counter.read_text().count("x") == 1

    (repo / "other.txt").write_text("more")
    sh("git add -A && git commit -qm more", repo)

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert created.setup is None  # no lockfile changed, setup skipped
    assert counter.read_text().count("x") == 1
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == git.out(
        ["rev-parse", "main"], str(repo)
    )


def test_pool_size_zero_disables_pool(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'], pool_size=0)
    cfg = load_repo_config(str(repo))
    assert cfg.pool_size == 0
    assert pool.fill(db, str(repo)) == 0

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert created.setup is not None and created.setup.ok
    assert counter.read_text().count("x") == 1


def test_failed_claim_falls_back(db, repo, tmp_path):
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    entry = pool.fill_one(db, str(repo))
    assert entry is not None
    shutil.rmtree(entry.path, ignore_errors=True)  # corrupt the entry on disk

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert created.setup is not None and created.setup.ok
    assert counter.read_text().count("x") == 2  # once during fill, once via fallback
    assert db.count_pool_entries(str(repo), "main") == 0


def test_pool_entries_do_not_appear_in_workspace_listings(db, repo):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.find_workspaces(str(repo)) == []
    assert view.snapshot(db, str(repo)) == []


def test_prune_removes_entries_when_pool_size_drops(db, repo):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    write_config(repo, setup=["true"], pool_size=0)
    sessions.enforce(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 0
