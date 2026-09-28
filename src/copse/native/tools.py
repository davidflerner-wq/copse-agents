"""The native worker's tools. Named as Claude Code names them, so a
profile's ``allowed_tools`` reads the same for either provider.

Every file tool is confined to the worker's directory (its worktree): a
path that resolves outside it is refused. Bash runs there too, with a
timeout and a cap on captured output, and its exit code is reported so the
model doesn't have to guess.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from copse.native.client import ToolSpec

MAX_OUTPUT = 30_000       # characters of any tool result kept for the model
MAX_MATCHES = 200
MAX_FILES = 500
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".copse", "dist", "build"}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], ToolResult]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


@dataclass
class Toolbox:
    tools: dict[str, Tool] = field(default_factory=dict)
    max_output: int = MAX_OUTPUT

    def add(self, *tools: Tool) -> "Toolbox":
        for t in tools:
            self.tools[t.name] = t
        return self

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self.tools.values()]

    def call(self, name: str, args: dict) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"unknown tool {name!r}; available: {', '.join(self.tools)}", True)
        try:
            result = tool.run(args)
        except Exception as e:  # a tool bug must not end the worker
            return ToolResult(f"{name} failed: {type(e).__name__}: {e}", True)
        result.content = clip(result.content, self.max_output)
        return result


def clip(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` characters, keeping the start and the
    end (the error is usually at the end, the context at the start)."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    dropped = len(text) - head - tail
    return f"{text[:head]}\n\n[... {dropped} characters cut ...]\n\n{text[-tail:]}"


# -- the core tools -----------------------------------------------------------

def core_tools(cwd: str, bash_timeout: float = 300.0) -> list[Tool]:
    root = Path(cwd).resolve()

    def resolve(raw: str) -> Path:
        p = (root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        if p != root and root not in p.parents:
            raise PermissionError(f"{raw} is outside the working directory {root}")
        return p

    def read(args: dict) -> ToolResult:
        path = resolve(str(args.get("path") or ""))
        if not path.is_file():
            return ToolResult(f"no such file: {args.get('path')}", True)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return ToolResult(f"{args.get('path')} isn't a text file", True)
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args.get("limit") or 2000)
        chunk = lines[offset - 1:offset - 1 + limit]
        if not chunk and lines:
            return ToolResult(f"offset {offset} is past the end ({len(lines)} lines)", True)
        body = "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(chunk, offset))
        if offset - 1 + limit < len(lines):
            body += f"\n[{len(lines) - (offset - 1 + limit)} more lines; read with offset={offset + limit}]"
        return ToolResult(body or "(empty file)")

    def write(args: dict) -> ToolResult:
        path = resolve(str(args.get("path") or ""))
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult("content must be a string", True)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        return ToolResult(f"{'updated' if existed else 'created'} {args.get('path')} ({len(content.splitlines())} lines)")

    def edit(args: dict) -> ToolResult:
        path = resolve(str(args.get("path") or ""))
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return ToolResult("old_string and new_string must be strings", True)
        if not old:
            return ToolResult("old_string is empty; use Write to create a file", True)
        if not path.is_file():
            return ToolResult(f"no such file: {args.get('path')}", True)
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            hint = _near_miss(text, old)
            return ToolResult("old_string was not found in the file. It must match exactly, including "
                              "whitespace and indentation." + (f" Closest line: {hint!r}" if hint else ""), True)
        if count > 1 and not args.get("replace_all"):
            return ToolResult(f"old_string appears {count} times; include more surrounding lines to make "
                              "it unique, or pass replace_all=true", True)
        if old == new:
            return ToolResult("old_string and new_string are the same", True)
        path.write_text(text.replace(old, new), encoding="utf-8")
        return ToolResult(f"edited {args.get('path')}: {count} replacement{'s' if count > 1 else ''}")

    def glob_(args: dict) -> ToolResult:
        pattern = str(args.get("pattern") or "**/*")
        base = resolve(str(args.get("path") or "."))
        found = []
        for p in sorted(base.glob(pattern)):
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if p.is_file():
                found.append(str(p.relative_to(root)))
            if len(found) >= MAX_FILES:
                found.append(f"[stopped at {MAX_FILES} files; narrow the pattern]")
                break
        return ToolResult("\n".join(found) or "no files match")

    def grep(args: dict) -> ToolResult:
        try:
            rx = re.compile(str(args.get("pattern") or ""), re.I if args.get("ignore_case") else 0)
        except re.error as e:
            return ToolResult(f"bad regex: {e}", True)
        base = resolve(str(args.get("path") or "."))
        name_glob = args.get("glob")
        files = [base] if base.is_file() else sorted(
            p for p in base.rglob(name_glob or "*")
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts))
        hits: list[str] = []
        for f in files:
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(root)}:{i}:{line.strip()[:300]}")
                        if len(hits) >= MAX_MATCHES:
                            hits.append(f"[stopped at {MAX_MATCHES} matches; narrow the search]")
                            return ToolResult("\n".join(hits))
            except (UnicodeDecodeError, OSError):
                continue
        return ToolResult("\n".join(hits) or "no matches")

    def bash(args: dict) -> ToolResult:
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult("command is empty", True)
        timeout = min(float(args.get("timeout") or bash_timeout), bash_timeout)
        try:
            proc = subprocess.run(command, shell=True, cwd=str(root), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, errors="replace", timeout=timeout,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat"})
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return ToolResult(f"{out}\n[timed out after {int(timeout)}s]", True)
        out = proc.stdout.rstrip("\n")
        if proc.stderr.strip():
            out += ("\n" if out else "") + proc.stderr.rstrip("\n")
        if proc.returncode != 0:
            return ToolResult(f"{out}\n[exit code {proc.returncode}]".lstrip("\n"), True)
        return ToolResult(out or "(no output)")

    return [
        Tool("Read", "Read a text file with line numbers. Long files are paged: use offset and limit.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "description": "Path relative to the working directory"},
                 "offset": {"type": "integer", "description": "First line to read (1-based)"},
                 "limit": {"type": "integer", "description": "How many lines (default 2000)"}},
              "required": ["path"]}, read),
        Tool("Write", "Create or overwrite a file with the given content.",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
              "required": ["path", "content"]}, write),
        Tool("Edit", "Replace an exact string in a file. old_string must match exactly once (including "
             "whitespace); include surrounding lines to make it unique, or set replace_all.",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "old_string": {"type": "string"},
                 "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
              "required": ["path", "old_string", "new_string"]}, edit),
        Tool("Glob", "List files matching a glob pattern, e.g. src/**/*.py.",
             {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "path": {"type": "string", "description": "Directory to search (default .)"}},
              "required": ["pattern"]}, glob_),
        Tool("Grep", "Search file contents with a regular expression. Returns path:line:text.",
             {"type": "object", "properties": {
                 "pattern": {"type": "string"}, "path": {"type": "string", "description": "File or directory (default .)"},
                 "glob": {"type": "string", "description": "Only files matching this name pattern, e.g. *.py"},
                 "ignore_case": {"type": "boolean"}},
              "required": ["pattern"]}, grep),
        Tool("Bash", "Run a shell command in the working directory and return its output and exit code. "
             "Commands can't read stdin.",
             {"type": "object", "properties": {
                 "command": {"type": "string"},
                 "timeout": {"type": "number", "description": "Seconds (default and cap: the worker's limit)"}},
              "required": ["command"]}, bash),
    ]


def _near_miss(text: str, old: str) -> str | None:
    """The line in ``text`` most like the first line of ``old``, to show the
    model what it probably meant (whitespace mismatches are the usual cause)."""
    first = old.strip().splitlines()[0].strip() if old.strip() else ""
    if not first:
        return None
    for line in text.splitlines():
        if line.strip() == first:
            return line
    return None
