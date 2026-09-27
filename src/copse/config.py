"""Paths and per-repo configuration.

Repo config lives in ``<repo>/.copse/config.json`` (committed, shared with the
team) and ``<repo>/.copse/config.local.json`` (gitignored, personal). Local
keys override shared ones; for command lists, local may instead give
``{"before": [...], "after": [...]}`` to wrap the team's commands.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = ".copse"
CONFIG_FILE = "config.json"
LOCAL_CONFIG_FILE = "config.local.json"

PORT_RANGE_START = 20000
PORT_BLOCK_SIZE = 10


def copse_home() -> Path:
    return Path(os.environ.get("COPSE_HOME", Path.home() / ".copse"))


def db_path() -> Path:
    return copse_home() / "copse.db"


def worktrees_dir() -> Path:
    return copse_home() / "worktrees"


def user_profiles_dir() -> Path:
    return copse_home() / "agents"


@dataclass
class RepoConfig:
    setup: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    base_branch: str | None = None
    branch_prefix: str = ""
    default_agent: str = "developer"
    fetch: bool = True


def _merge_commands(shared: list[str], local: object) -> list[str]:
    if isinstance(local, list):
        return [str(c) for c in local]
    if isinstance(local, dict):
        return [*local.get("before", []), *shared, *local.get("after", [])]
    return shared


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    base = Path(repo_root) / CONFIG_DIR
    shared = _read_json(base / CONFIG_FILE)
    local = _read_json(base / LOCAL_CONFIG_FILE)

    cfg = RepoConfig()
    for key in ("setup", "teardown", "copy"):
        merged = _merge_commands(list(shared.get(key, [])), local.get(key))
        setattr(cfg, key, merged)
    for key in ("base_branch", "branch_prefix", "default_agent", "fetch"):
        if key in local:
            setattr(cfg, key, local[key])
        elif key in shared:
            setattr(cfg, key, shared[key])
    return cfg


TEMPLATE = {
    "setup": [],
    "teardown": [],
    "copy": [".env"],
    "base_branch": None,
    "branch_prefix": "",
    "default_agent": "developer",
    "fetch": True,
}


def write_template(repo_root: str | Path) -> Path:
    base = Path(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / CONFIG_FILE
    if not path.exists():
        path.write_text(json.dumps(TEMPLATE, indent=2) + "\n", encoding="utf-8")
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path
