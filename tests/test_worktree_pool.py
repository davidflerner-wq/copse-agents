import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from copse import git, pool, sessions, view, workspaces
from copse.config import load_repo_config
from copse.db import DB, PoolEntry

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
    assert pool.trim(db, str(repo)) == 1
    assert db.count_pool_entries(str(repo), "main") == 0


def test_enforce_kicks_off_a_background_fill_instead_of_trimming_inline(db, repo, monkeypatch):
    # sessions.enforce must never trim synchronously (a large trim's rmtree
    # would block whoever's starting up); it just kicks off the same
    # detached fill process a claim does, which sweeps/trims/fills itself.
    calls = []
    monkeypatch.setattr(pool, "fill_in_background", lambda repo_root: calls.append(repo_root))
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    write_config(repo, setup=["true"], pool_size=0)
    sessions.enforce(db, str(repo))
    assert calls == [str(repo)]
    assert db.count_pool_entries(str(repo), "main") == 1  # untouched: enforce doesn't trim inline


def test_claim_skipped_when_branch_already_exists_locally(db, repo, tmp_path):
    """A pool claim must never `checkout -B` an existing branch: that would
    reset it onto the pool's base sha and discard its commits."""
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    other = tmp_path / "other-checkout"
    sh(f"git worktree add -q -b feature {other} main", repo)
    (other / "own_work.txt").write_text("precious")
    sh("git add -A && git commit -qm 'own work'", other)
    own_sha = sh("git rev-parse feature", other)
    sh(f"git worktree remove --force {other}", repo)

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == own_sha
    assert (Path(created.workspace.path) / "own_work.txt").exists()
    assert db.count_pool_entries(str(repo), "main") == 1  # entry left untouched


def test_claim_skipped_when_branch_exists_only_on_remote(db, repo, tmp_path):
    """A branch that only exists on origin must be checked out tracking it,
    not replaced by a pool entry sitting at a different (local base) commit."""
    counter = tmp_path / "counter"
    write_config(repo, setup=[f'echo x >> "{counter}"'])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    other = tmp_path / "other-checkout"
    sh(f"git worktree add -q -b feature {other} main", repo)
    (other / "remote_work.txt").write_text("from origin")
    sh("git add -A && git commit -qm 'remote work'", other)
    sh("git push -q origin feature", other)
    remote_sha = sh("git rev-parse feature", other)
    sh(f"git worktree remove --force {other}", repo)
    sh("git branch -D feature", repo)
    sh("git fetch -q origin", repo)
    assert git.remote_branch_exists(str(repo), "feature")
    assert not git.branch_exists(str(repo), "feature")

    created = workspaces.create(db, str(repo), "feature")
    assert created.how != "pool"
    assert git.out(["rev-parse", "HEAD"], created.workspace.path) == remote_sha
    assert (Path(created.workspace.path) / "remote_work.txt").exists()
    assert db.count_pool_entries(str(repo), "main") == 1  # entry left untouched


def test_venv_from_setup_survives_a_claim(db, repo):
    """The entry is built and stays at its final path/port forever, so a venv
    (or anything else with the path baked in) setup created must keep
    working after a claim, with no `git worktree move` in between."""
    write_config(repo, setup=[
        "python3 -m venv .venv",
        'echo "$COPSE_WORKSPACE_PATH" > seen_path.txt',
        'echo "$COPSE_PORT_BASE" > seen_port.txt',
    ])
    entry = pool.fill_one(db, str(repo))
    assert entry is not None
    assert (Path(entry.path) / "seen_path.txt").read_text().strip() == entry.path
    assert (Path(entry.path) / "seen_port.txt").read_text().strip() == str(entry.port_base)

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert created.workspace.path == entry.path  # never moved
    assert created.workspace.port_base == entry.port_base  # never reassigned

    venv_python = Path(created.workspace.path) / ".venv" / "bin" / "python"
    assert venv_python.is_file()
    subprocess.run([str(venv_python), "-c", "pass"], check=True)


def test_claim_recopies_changed_copy_files(db, repo):
    write_config(repo, setup=["true"], copy=[".env"])
    entry = pool.fill_one(db, str(repo))
    assert entry is not None
    assert (Path(entry.path) / ".env").read_text() == "SECRET=1\n"

    (repo / ".env").write_text("SECRET=2\n")

    created = workspaces.create(db, str(repo), "feature")
    assert created.how == "pool"
    assert (Path(created.workspace.path) / ".env").read_text() == "SECRET=2\n"
    assert ".env" in created.copied


def test_sweep_removes_a_crashed_fills_leftovers(db, repo, tmp_path):
    write_config(repo, setup=["true"])
    cfg = load_repo_config(str(repo))

    # A fill that inserted its row and built the worktree, then crashed
    # before setup finished -- so it never flipped ready=1.
    base_sha = git.out(["rev-parse", "main"], str(repo))
    token = "deadbeef"
    branch = f"copse-pool/{token}"
    path = str(pool.pool_dir(str(repo)) / token)
    entry = PoolEntry(
        path=path, repo_root=str(repo), base_branch="main", base_sha=base_sha,
        branch=branch, fingerprint=pool.fingerprint(str(repo), base_sha, cfg),
        port_base=40000, ready=0, created_at=time.time(),
    )
    db.add_pool_entry(entry)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    git.run(["worktree", "add", "--no-track", "-b", branch, path, base_sha], str(repo))

    assert len(db.pool_entries(str(repo), ready_only=False)) == 1

    assert pool.fill_locked(db, str(repo)) == 1  # sweeps the crash, then fills fresh

    entries = db.pool_entries(str(repo), "main")
    assert len(entries) == 1
    assert entries[0].path != path
    assert not os.path.exists(path)
    assert not git.branch_exists(str(repo), branch)


def test_sweep_drops_entries_for_an_unconfigured_base_branch(db, repo, tmp_path):
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    sh("git checkout -q -b other", repo)
    sh("git push -q -u origin other", repo)
    write_config(repo, setup=["true"], base_branch="other")

    assert pool.fill_locked(db, str(repo)) == 1
    assert db.count_pool_entries(str(repo), "main") == 0
    assert db.count_pool_entries(str(repo), "other") == 1


def test_two_concurrent_claims_only_one_wins(db, repo):
    """take_pool_entry uses BEGIN IMMEDIATE, so two connections racing to
    claim the same entry must never both succeed."""
    write_config(repo, setup=["true"])
    pool.fill(db, str(repo))
    assert db.count_pool_entries(str(repo), "main") == 1

    results: list = []
    barrier = threading.Barrier(2)

    def take():
        conn = DB()  # its own connection, created (and used) in this thread
        barrier.wait()
        results.append(conn.take_pool_entry(str(repo), "main"))

    threads = [threading.Thread(target=take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r is not None]
    assert len(won) == 1
    assert db.count_pool_entries(str(repo), "main") == 0


def test_fill_backs_off_after_a_setup_failure(db, repo, monkeypatch):
    write_config(repo, setup=["false"])  # always fails
    assert pool.fill(db, str(repo)) == 0
    assert db.last_pool_failure(str(repo), "main") is not None

    calls: list = []
    real_fill_one = pool.fill_one
    monkeypatch.setattr(
        pool, "fill_one", lambda *a, **kw: (calls.append(1), real_fill_one(*a, **kw))[1]
    )

    assert pool.fill(db, str(repo)) == 0
    assert calls == []  # still inside the backoff window: never even tried

    monkeypatch.setattr(pool, "FAILURE_BACKOFF", 0)
    assert pool.fill(db, str(repo)) == 0
    assert calls == [1]  # backoff expired: retried (and failed again)
