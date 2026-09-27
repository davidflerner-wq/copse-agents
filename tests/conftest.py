import subprocess
from pathlib import Path

import pytest

from copse.db import DB


def sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def copse_home(tmp_path, monkeypatch):
    home = tmp_path / "copse-home"
    monkeypatch.setenv("COPSE_HOME", str(home))
    for k in ("GIT_DIR", "GIT_WORK_TREE", "COPSE_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    return home


@pytest.fixture
def db(copse_home):
    return DB()


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with one commit, pushed to a bare ``origin``."""
    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    sh("git add -A && git commit -qm init", work)
    sh(f"git remote add origin {origin} && git push -q -u origin main", work)
    sh("git remote set-head origin main", work)
    return work
