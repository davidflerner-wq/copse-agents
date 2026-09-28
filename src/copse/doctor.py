"""``copse doctor``: what copse needs, and whether it's there.

Plain Claude Code needs nothing set up; copse needs tmux, the agent CLIs,
a writable home, and (per repo) a few optional pieces. This checks each one
and says what to do about anything missing, so a first run doesn't fail
halfway through a launch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[-1].strip() if text else ""


def _tool(name: str, required: bool, why: str, version_args: list[str] | None = None,
          install: str = "") -> Check:
    path = shutil.which(name)
    if not path:
        level = FAIL if required else WARN
        return Check(level, name, f"not found: {why}." + (f" Install: {install}" if install else ""))
    version = _version([path, *(version_args or ["--version"])]) or ""
    return Check(OK, name, f"{path}" + (f" ({version[:40]})" if version else ""))


def checks(repo_root: str | None) -> list[Check]:
    from copse import config, procs, tmux
    from copse.db import DB

    out: list[Check] = []
    v = sys.version_info
    out.append(Check(OK if v >= (3, 11) else FAIL, "python",
                     f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (copse needs 3.11 or newer)")))
    out.append(_tool("tmux", True, "copse runs every agent in a tmux window", ["-V"], "brew install tmux"))
    out.append(_tool("claude", True, "the supervisor and the built-in profiles use Claude Code",
                     install="see https://code.claude.com"))
    out.append(_tool("codex", False, "only needed for Codex agents (reviewer-codex)"))
    out.append(_tool("agy", False, "only needed for Google Antigravity agents"))
    out.append(_tool("gh", False, "only needed for `copse pr` and `copse new --pr`"))
    out.append(_tool("pre-commit", False, "only needed if the repo uses pre-commit hooks"))
    out.append(_tool("graphify", False, "only needed for the code map agents can query"))

    home = config.copse_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        DB().conn.execute("SELECT 1")
        out.append(Check(OK, "copse home", f"{home} (writable, database opens)"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(FAIL, "copse home", f"{home}: {e}"))

    if shutil.which("tmux"):
        try:
            sock = os.environ.get("COPSE_TMUX_SOCKET")
            cmd = ["tmux", *(["-L", sock] if sock else []), "-V"]
            subprocess.run(cmd, capture_output=True, timeout=10)
            focus = tmux._tmux("show-options", "-gs", "focus-events", check=False).stdout.strip()
            out.append(Check(OK, "tmux focus-events", focus or "not set yet (copse sets it at launch)"))
        except Exception as e:  # noqa: BLE001
            out.append(Check(WARN, "tmux focus-events", str(e)))

    try:
        db = DB()
        table = procs.table()
        stray = []
        for aid in procs.all_agent_ids(table):
            a = db.get_agent(aid)
            if a is None or a.status in ("paused", "done") or a.dismissed_at is not None:
                stray.append(aid)
        if stray:
            out.append(Check(WARN, "leftover processes",
                             f"{len(stray)} agent(s) have processes running but aren't active: "
                             f"{', '.join(stray[:5])}. `copse prune` stops them."))
        else:
            out.append(Check(OK, "leftover processes", "none"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(WARN, "leftover processes", f"couldn't check: {e}"))

    if repo_root:
        try:
            cfg = config.load_repo_config(repo_root)
            path = Path(repo_root) / config.CONFIG_DIR / config.CONFIG_FILE
            if path.exists():
                out.append(Check(OK, "repo config", f"{path}"))
            else:
                out.append(Check(OK, "repo config", "none (defaults apply; `copse init` writes a starter)"))
            if cfg.checks:
                out.append(Check(OK, "checks", "; ".join(cfg.checks)))
            else:
                out.append(Check(WARN, "checks",
                                 "none configured: nothing verifies a branch before it merges. "
                                 'Add e.g. {"checks": ["uv run pytest -q"]} to .copse/config.json'))
            if (Path(repo_root) / "graphify-out" / "graph.json").is_file():
                out.append(Check(OK, "code map", "graphify-out/graph.json"))
            else:
                out.append(Check(WARN, "code map",
                                 "no graphify graph: agents find code by grepping. Run /graphify once."))
        except ValueError as e:
            out.append(Check(FAIL, "repo config", str(e)))
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            out.append(Check(WARN, "working tree",
                             "uncommitted changes: workers branch from committed work only"))
        else:
            out.append(Check(OK, "working tree", "clean"))
    return out


MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def render(results: list[Check]) -> str:
    width = max(len(c.name) for c in results) if results else 0
    lines = [f"{MARK[c.level]} {c.name.ljust(width)}  {c.detail}" for c in results]
    fails = sum(c.level == FAIL for c in results)
    warns = sum(c.level == WARN for c in results)
    if fails:
        lines.append(f"\n{fails} problem(s) will stop copse from working; {warns} warning(s).")
    elif warns:
        lines.append(f"\ncopse can run; {warns} warning(s) worth a look.")
    else:
        lines.append("\nAll good.")
    return "\n".join(lines)
