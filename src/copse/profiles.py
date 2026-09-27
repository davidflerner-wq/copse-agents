"""Agent profiles: markdown files with a small frontmatter header.

    ---
    name: developer
    description: Implements a well-scoped coding task
    provider: claude
    ---
    You are a developer agent...

Lookup order: ``<repo>/.copse/agents/``, ``~/.copse/agents/``, then the
built-in profiles shipped with copse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from copse.config import CONFIG_DIR, user_profiles_dir


@dataclass
class Profile:
    name: str
    description: str
    provider: str
    prompt: str
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    # Claude Code only; all off by default (see README, "Cheap workers").
    strict_mcp: bool = False                 # --strict-mcp-config: only copse's MCP server
    setting_sources: list[str] | None = None  # --setting-sources, e.g. project,local
    effort: str | None = None                # --effort low|medium|high|xhigh|max
    headless: bool = False                   # run with `claude -p`, turn by turn


_COMMENT = re.compile(r"(?:^|\s)#.*$")


def _value(raw: str) -> str:
    """A frontmatter value without its trailing ``# comment``. As in YAML, a
    ``#`` only starts a comment at the start or after whitespace, and a quoted
    value is taken as is."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return _COMMENT.sub("", raw).strip()


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("true", "yes", "on", "1")


def _list(value: str | None) -> list[str] | None:
    """A comma-separated list; YAML's ``[a, b]`` form works too."""
    value = (value or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = [t.strip().strip("'\"") for t in value.split(",") if t.strip()]
    return items or None


def _parse(text: str, fallback_name: str) -> Profile:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, header, body = text.split("---", 2)
        for line in header.strip().splitlines():
            if line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip()] = _value(value)
    return Profile(
        name=meta.get("name", fallback_name),
        description=meta.get("description", ""),
        provider=meta.get("provider", "claude"),
        prompt=body.strip(),
        model=meta.get("model") or None,
        permission_mode=meta.get("permission_mode") or None,
        allowed_tools=_list(meta.get("allowed_tools")),
        strict_mcp=_flag(meta.get("strict_mcp")),
        setting_sources=_list(meta.get("setting_sources")),
        effort=meta.get("effort") or None,
        headless=_flag(meta.get("headless")),
    )


def _search_dirs(repo_root: str | None) -> list[Path]:
    dirs = []
    if repo_root:
        dirs.append(Path(repo_root) / CONFIG_DIR / "agents")
    dirs.append(user_profiles_dir())
    return dirs


def load_profile(name: str, repo_root: str | None = None) -> Profile:
    for d in _search_dirs(repo_root):
        f = d / f"{name}.md"
        if f.is_file():
            return _parse(f.read_text(encoding="utf-8"), name)
    builtin = resources.files("copse.builtin_agents").joinpath(f"{name}.md")
    if builtin.is_file():
        return _parse(builtin.read_text(encoding="utf-8"), name)
    raise KeyError(f"no agent profile named {name!r}")


def list_profiles(repo_root: str | None = None) -> list[Profile]:
    seen: dict[str, Profile] = {}
    builtin_dir = resources.files("copse.builtin_agents")
    for entry in builtin_dir.iterdir():
        if entry.name.endswith(".md"):
            p = _parse(entry.read_text(encoding="utf-8"), entry.name[:-3])
            seen[p.name] = p
    for d in reversed(_search_dirs(repo_root)):
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                p = _parse(f.read_text(encoding="utf-8"), f.stem)
                seen[p.name] = p
    return sorted(seen.values(), key=lambda p: p.name)
