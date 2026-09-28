"""A code map for agents: the repo's graphify knowledge graph, if it has one.

Finding the code a task touches is much of what an agent spends tokens on:
listing folders, grepping, reading whole files to find one function.
``graphify query "<question>"`` answers from a prebuilt graph of the repo
instead, listing the relevant symbols with file and line, in well under a
second. When the repo has ``graphify-out/graph.json`` and graphify is
installed, copse tells its agents to ask the graph first and read only what
it points to.

Workers query the graph of the main checkout (their own worktree doesn't
have ``graphify-out/``, which is usually ignored); its paths are relative to
the repo, so they hold in any worktree. After each merge copse refreshes the
graph in the background with ``graphify update``, which re-extracts code
without an LLM, so the next workers see the merged code.

``graphify`` in ``.copse/config.json``: true or false to force it on or off;
unset, it's on whenever the graph and the command are there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

GRAPH = Path("graphify-out") / "graph.json"
# Read-only graphify commands, pre-approved for Claude agents.
ALLOWED_TOOLS = ["Bash(graphify query:*)", "Bash(graphify explain:*)", "Bash(graphify path:*)"]


def graph_path(repo_root: str) -> Path | None:
    """The repo's graph, if agents should use it."""
    from copse.config import load_repo_config

    try:
        setting = load_repo_config(repo_root).graphify
    except ValueError:
        setting = None
    path = Path(repo_root) / GRAPH
    if setting is False or not path.is_file() or not shutil.which("graphify"):
        return None
    return path


def guidance(repo_root: str) -> str | None:
    """What to tell an agent about the code map, or None if there isn't one."""
    path = graph_path(repo_root)
    if path is None:
        return None
    return (
        "Code map: this repo has a graphify knowledge graph. To find where something "
        "lives or what calls what, ask it before listing folders, grepping or reading "
        f"whole files: `graphify query \"<question>\" --graph {path} --budget 1500` lists "
        "the relevant functions and classes with file and line (`graphify explain "
        f"\"<name>\" --graph {path}` for one symbol). Then read just those lines. The map "
        "can lag behind the code a little, so trust the files when they differ."
    )


def refresh_later(repo_root: str) -> None:
    """Rebuild the code part of the graph in the background, after a merge."""
    if graph_path(repo_root) is None:
        return
    from copse.config import copse_home

    log = copse_home() / "graphify.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as out:
        subprocess.Popen(["graphify", "update", repo_root], cwd=repo_root,
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
