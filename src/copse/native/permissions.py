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
        if tool.startswith("mcp__copse") or tool in READ_ONLY:
            return "allow"  # a Read(...) rule narrows nothing: reads are always fine
        if self.mode == "bypassPermissions" or self._allowed_by_rule(tool, args):
            return "allow"
        if tool in EDITS and self.mode in ("acceptEdits", "auto"):
            return "allow"
        if self.mode in ("dontAsk", "plan"):
            return "deny"
        return "ask"

    def _allowed_by_rule(self, tool: str, args: dict) -> bool:
        for name, spec in self.rules:
            if name != tool:
                continue
            if spec is None or spec == "" or spec == "*":
                return True
            if tool == "Bash":
                if bash_matches(spec, str(args.get("command", ""))):
                    return True
            else:
                target = str(args.get("path") or args.get("file_path") or args.get("pattern") or "")
                if fnmatch.fnmatchcase(target, spec) or spec.endswith(":*") and target.startswith(spec[:-2]):
                    return True
        return False


def _canonical(mode: str | None) -> str:
    m = (mode or "default").replace("-", "").lower()
    return {"acceptedits": "acceptEdits", "bypasspermissions": "bypassPermissions",
            "dontask": "dontAsk", "auto": "auto", "plan": "plan"}.get(m, "default")


def bash_matches(spec: str, command: str) -> bool:
    """Whether every simple command in ``command`` is covered by ``spec``
    (``prefix:*`` or an exact command). Command substitution is never
    covered: what it runs can't be seen from here."""
    command = command.strip()
    if not command or "$(" in command or "`" in command:
        return False
    parts = split_commands(command)
    if not parts:
        return False
    for part in parts:
        if spec.endswith(":*"):
            prefix = spec[:-2].strip()
            if not (part == prefix or part.startswith(prefix + " ")):
                return False
        elif part != spec.strip():
            return False
    return True


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
