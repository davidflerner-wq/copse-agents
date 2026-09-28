"""The docs cover every command and tool copse actually has.

Checks README.md always, and the pawdelta.com/copse page too when
COPSE_SITE_PAGE points at its HTML source.
"""

import asyncio
import html
import os
import re
from pathlib import Path

import pytest
import typer

from copse.cli import app
from copse.mcp_server import mcp

README = Path(__file__).resolve().parent.parent / "README.md"


def _commands() -> list[str]:
    """Every public command, e.g. "ls" and "agent spawn"."""
    found = []

    def walk(cmd, prefix: str) -> None:
        if hasattr(cmd, "commands"):
            for name, sub in cmd.commands.items():
                if not sub.hidden:
                    walk(sub, f"{prefix} {name}".strip())
        elif prefix:
            found.append(prefix)

    walk(typer.main.get_command(app), "")
    return sorted(found)


def _tools() -> list[str]:
    return sorted(t.name for t in asyncio.run(mcp.list_tools()))


def _text(path: Path) -> str:
    raw = path.read_text()
    if path.suffix == ".html":
        raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return raw


def _pages() -> list[Path]:
    pages = [README]
    site = os.environ.get("COPSE_SITE_PAGE")
    if site:
        pages.append(Path(site))
    return pages


def _mentions_command(text: str, cmd: str) -> bool:
    """`copse ls`, or a grouped form like `copse attach / cd / open` or
    `copse agent spawn/kill/peek`."""
    *group, name = cmd.split()
    prefix = "copse " + "".join(f"{g} " for g in group)
    for m in re.finditer(rf"{prefix}[\w-]+(?: ?/ ?[\w-]+)*", text):
        if name in re.split(r" ?/ ?", m.group(0)[len(prefix):]):
            return True
    return False


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_command_is_documented(page):
    text = _text(page)
    missing = [c for c in _commands() if not _mentions_command(text, c)]
    assert not missing, f"{page.name} doesn't document: {', '.join(missing)}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_every_agent_tool_is_documented(page):
    text = _text(page)
    missing = [t for t in _tools() if not re.search(rf"\b{t}\b", text)]
    assert not missing, f"{page.name} doesn't mention MCP tools: {', '.join(missing)}"


@pytest.mark.parametrize("page", _pages(), ids=lambda p: p.name)
def test_recommended_use_section(page):
    assert re.search(r"Recommended (use|workflow)", _text(page)), \
        f"{page.name} has no recommended-use section"


def test_command_help_has_no_hard_wraps():
    """Docstring line breaks mid-sentence show up as ragged lines in --help."""
    ragged = []

    def walk(cmd, name: str) -> None:
        if hasattr(cmd, "commands"):
            for sub_name, sub in cmd.commands.items():
                if not sub.hidden:
                    walk(sub, f"{name} {sub_name}".strip())
        first_para = (cmd.help or "").strip().split("\n\n")[0]
        if name and "\n" in first_para:
            ragged.append(name)

    walk(typer.main.get_command(app), "")
    assert not ragged, f"summary paragraph spans lines in: {', '.join(ragged)}"
