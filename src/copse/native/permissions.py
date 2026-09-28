"""``permission_mode`` and ``allowed_tools`` for the native loop.

The shapes are Claude Code's, so a profile reads the same whichever provider
runs it: ``Bash(git add:*)`` allows commands starting with ``git add``,
``Bash(pytest)`` exactly ``pytest``, ``Edit`` every edit, ``Read(src/*)``
reads matching a glob. Copse's own tools are always allowed.

Modes: ``bypassPermissions`` allows everything; ``acceptEdits`` and ``auto``
allow file changes (there's no classifier here, so ``auto`` is
``acceptEdits`` with the allowlist); ``dontAsk``, ``plan`` and the default
allow only reads and what ``allowed_tools`` names. A tool that would need a
person's answer comes back as ``ask``; the loop decides what to do with that
(refuse, for an unattended worker).
"""

from __future__ import annotations

import fnmatch
import re
import shlex

READ_ONLY = {"Read", "Glob", "Grep"}
# copse's own tools, as the native loop names them (the MCP server's names
# are the same, prefixed mcp__copse__).
COPSE_TOOLS = {"report_result", "submit_review", "send_message", "workspace_diff"}
EDITS = {"Edit", "Write"}
_RULE = re.compile(r"^(\w+)(?:\((.*)\))?$")
_SEPARATORS = {"&&", "||", ";", "|", "&"}


class Permissions:
    def __init__(self, mode: str | None, allowed: list[str] | None):
        self.mode = _canonical(mode)
        self.rules: list[tuple[str, str | None]] = []
        for raw in allowed or []:
            m = _RULE.match(raw.strip())
            if m:
                self.rules.append((m.group(1), m.group(2)))

    def decide(self, tool: str, args: dict) -> str:
        """'allow', 'deny' or 'ask'."""
        if tool.startswith("mcp__copse") or tool in COPSE_TOOLS or tool in READ_ONLY:
            return "allow"  # a Read(...) rule narrows nothing: reads are always fine
        if self.mode == "bypassPermissions" or self._allowed_by_rule(tool, args):
            return "allow"
        if tool in EDITS and self.mode in ("acceptEdits", "auto"):
            return "allow"
        if self.mode in ("dontAsk", "plan"):
            return "deny"
        return "ask"

    def _allowed_by_rule(self, tool: str, args: dict) -> bool:
        specs = [spec for name, spec in self.rules if name == tool]
        if not specs:
            return False
        if any(spec in (None, "", "*") for spec in specs):
            return True
        if tool == "Bash":
            return uncovered_part(specs, str(args.get("command", ""))) is None
        target = str(args.get("path") or args.get("file_path") or args.get("pattern") or "")
        return any(fnmatch.fnmatchcase(target, spec) or spec.endswith(":*") and target.startswith(spec[:-2])
                   for spec in specs)

    def reason(self, tool: str, args: dict) -> str:
        """Why a Bash command isn't allowed, for the model: the part no rule
        covers. Empty for other tools."""
        if tool != "Bash":
            return ""
        command = str(args.get("command", ""))
        if "$(" in command or "`" in command:
            return "command substitution ($(...) or backticks) is never allowed"
        specs = [spec for name, spec in self.rules if name == "Bash"]
        part = uncovered_part(specs, command)
        if part is None:
            return ""
        if part != command.strip():
            return f"`{part}` isn't covered by the allowed commands (each part of a compound command must be)"
        return "it isn't covered by the allowed commands"


def _canonical(mode: str | None) -> str:
    m = (mode or "default").replace("-", "").lower()
    return {"acceptedits": "acceptEdits", "bypasspermissions": "bypassPermissions",
            "dontask": "dontAsk", "auto": "auto", "plan": "plan"}.get(m, "default")


def bash_matches(spec: str, command: str) -> bool:
    """Whether every simple command in ``command`` is covered by ``spec``
    (``prefix:*`` or an exact command)."""
    return uncovered_part([spec], command) is None


def uncovered_part(specs: list[str | None], command: str, cd_root: str | None = None) -> str | None:
    """The first simple command in ``command`` (``a && b | c`` has three)
    that no spec in ``specs`` covers, or None when all are covered. Command
    substitution is never covered: what it runs can't be seen from here.
    Unparseable input (an unclosed quote) counts as uncovered. With
    ``cd_root``, a ``cd`` whose target is inside that directory is covered
    too (``cd sub && pytest`` from a worker's own worktree)."""
    command = command.strip()
    if not command or "$(" in command or "`" in command:
        return command or "(empty)"
    parts = split_commands(command)
    if not parts:
        return command
    cwd = cd_root
    for part in parts:
        if cd_root and _cd_inside(part, cwd, cd_root) is not None:
            cwd = _cd_inside(part, cwd, cd_root)
            continue
        if not any(_covers(spec, part) for spec in specs if spec):
            return part
    return None


def _cd_inside(part: str, cwd: str | None, root: str) -> str | None:
    """The directory ``part`` (a ``cd`` command) would land in, if that is
    ``root`` or below it; None for any other command or target."""
    tokens = part.split()
    if len(tokens) != 2 or tokens[0] != "cd" or tokens[1].startswith("-") or "$" in tokens[1]:
        return None  # a variable expands at run time to who knows where
    import os

    # realpath on both sides: a symlink inside the worktree may point out of
    # it, and the worktree itself may sit under one (/tmp on macOS).
    target = os.path.realpath(os.path.join(cwd or root, os.path.expanduser(tokens[1])))
    real_root = os.path.realpath(root)
    return target if target == real_root or target.startswith(real_root + os.sep) else None


def _covers(spec: str, part: str) -> bool:
    if spec.endswith(":*"):
        prefix = spec[:-2].strip()
        return part == prefix or part.startswith(prefix + " ")
    return part == spec.strip()


def split_commands(command: str) -> list[str]:
    """The simple commands joined by ``&&``, ``||``, ``;``, ``|`` or ``&``,
    each re-joined from its tokens (so spacing doesn't matter). Unparseable
    input (an unclosed quote) gives an empty list: nothing matches it."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []
    parts, current = [], []
    for tok in tokens:
        if tok in _SEPARATORS:
            if current:
                parts.append(" ".join(current))
            current = []
        else:
            current.append(tok)
    if current:
        parts.append(" ".join(current))
    return parts
