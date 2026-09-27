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


def _parse(text: str, fallback_name: str) -> Profile:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, header, body = text.split("---", 2)
        for line in header.strip().splitlines():
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip()] = value.strip()
    return Profile(
        name=meta.get("name", fallback_name),
        description=meta.get("description", ""),
        provider=meta.get("provider", "claude"),
        prompt=body.strip(),
        model=meta.get("model") or None,
        permission_mode=meta.get("permission_mode") or None,
        allowed_tools=[t.strip() for t in meta["allowed_tools"].split(",") if t.strip()]
        if meta.get("allowed_tools") else None,
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
