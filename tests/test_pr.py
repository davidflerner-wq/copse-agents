import os

import pytest
from typer.testing import CliRunner

from copse import git, workspaces
from copse.cli import app

from conftest import sh

runner = CliRunner()


@pytest.fixture
def pr_repo(repo, tmp_path):
    """``repo`` plus a ``release`` branch and a PR head branch ``fix/typo``
    (cut from release) that exist only on origin."""
    other = tmp_path / "other"
    sh(f"git clone -q {tmp_path / 'origin.git'} {other}", tmp_path)
    sh("git switch -qc release && git push -q origin release", other)
    sh("git switch -qc fix/typo", other)
    (other / "fix.txt").write_text("fixed\n")
    sh("git add -A && git commit -qm fix && git push -q origin fix/typo", other)
    return repo


def fake_gh(monkeypatch, head="fix/typo", base="release"):
    calls = []

    def view(repo_path, number):
        calls.append(number)
        return {"headRefName": head, "baseRefName": base}

    monkeypatch.setattr(workspaces, "gh_pr_view", view)
    return calls


def test_create_from_pr_checks_out_remote_head(db, pr_repo, monkeypatch):
    calls = fake_gh(monkeypatch)
    created = workspaces.create_from_pr(db, str(pr_repo), 42)
    ws = created.workspace
    assert calls == [42]
    assert created.how == "remote"
    assert ws.branch == "fix/typo" and ws.base_branch == "release"
    assert git.get_base(str(pr_repo), "fix/typo") == "release"
    assert open(os.path.join(ws.path, "fix.txt")).read() == "fixed\n"
    assert sh("git rev-parse --abbrev-ref @{upstream}", ws.path) == "origin/fix/typo"
    assert sh("git rev-parse HEAD", ws.path) == sh("git rev-parse origin/fix/typo", pr_repo)


def test_create_from_pr_ignores_branch_prefix(db, pr_repo, monkeypatch):
    (pr_repo / ".copse").mkdir()
    (pr_repo / ".copse" / "config.json").write_text('{"branch_prefix": "me/"}')
    fake_gh(monkeypatch)
    ws = workspaces.create_from_pr(db, str(pr_repo), 1).workspace
    assert ws.branch == "fix/typo"


def test_create_from_pr_missing_remote_branch(db, pr_repo, monkeypatch):
    fake_gh(monkeypatch, head="nope")
    with pytest.raises(workspaces.WorkspaceError, match="couldn't fetch PR #7"):
        workspaces.create_from_pr(db, str(pr_repo), 7)


def test_gh_failures_are_clear(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(workspaces.WorkspaceError, match="not installed"):
        workspaces.gh_pr_view(str(tmp_path), 1)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho 'no pull requests found' >&2\nexit 1\n")
    gh.chmod(0o755)
    with pytest.raises(workspaces.WorkspaceError, match="no pull requests found"):
        workspaces.gh_pr_view(str(tmp_path), 1)


def test_cli_new_pr(db, pr_repo, monkeypatch):
    fake_gh(monkeypatch)
    monkeypatch.chdir(pr_repo)
    result = runner.invoke(app, ["new", "--pr", "42", "--agent", "none"])
    assert result.exit_code == 0, result.output
    ws = workspaces.resolve(db, "fix/typo", cwd=str(pr_repo))
    assert ws.base_branch == "release"


def test_cli_new_requires_branch_without_pr(pr_repo, monkeypatch):
    monkeypatch.chdir(pr_repo)
    result = runner.invoke(app, ["new", "--agent", "none"])
    assert result.exit_code == 1
    assert "missing BRANCH" in result.output


@pytest.mark.parametrize("extra", [["feature"], ["--base", "main"]])
def test_cli_new_pr_rejects_branch_or_base(pr_repo, monkeypatch, extra):
    calls = fake_gh(monkeypatch)
    monkeypatch.chdir(pr_repo)
    result = runner.invoke(app, ["new", "--pr", "42", "--agent", "none", *extra])
    assert result.exit_code == 1
    assert "--pr" in result.output
    assert calls == []
