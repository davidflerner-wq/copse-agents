"""The turn loop: ask the model, run its tool calls, repeat until it stops.

``NativeAgent.run(prompt)`` is one turn in copse's sense (what a pasted
message starts in a wrapped CLI): the model works until it replies without
tool calls. Between model calls the loop drains ``inbox`` (messages queued
for the worker while it worked) and reports ``on_status``, so the provider
knows the worker's state without hooks or a screen to read.

Context is kept under ``LoopConfig.context_tokens`` (estimated at four
characters a token) in two steps: old tool results are trimmed first, and
if that isn't enough the middle of the conversation is folded into a
written summary of what was done, keeping the task and the latest turns
verbatim. Deterministic, so it never costs a model call or fails on a model
that summarizes badly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from copse.native.client import Client, Reply, ToolCall, Usage
from copse.native.permissions import Permissions
from copse.native.tools import Toolbox, ToolResult

DELIVERY_PREFIX = "copse delivered this message"
MAX_SUMMARY_LINES = 150


@dataclass
class LoopConfig:
    context_tokens: int = 32_000    # the model's window, less headroom for its reply
    keep_recent: int = 12           # messages kept verbatim when folding
    old_result_chars: int = 400     # how much of an old tool result survives trimming
    max_steps: int = 200            # model calls per run(); a runaway loop's ceiling
    continue_on_length: int = 2     # nudges after a reply cut off at max_tokens


@dataclass
class Step:
    reply: Reply
    results: list[tuple[ToolCall, ToolResult]] = field(default_factory=list)


class NativeAgent:
    def __init__(self, client: Client, toolbox: Toolbox, permissions: Permissions,
                 system_prompt: str, *, config: LoopConfig | None = None,
                 transcript: Path | str | None = None,
                 on_status: Callable[[str], None] | None = None,
                 inbox: Callable[[], list[str]] | None = None,
                 ask: Callable[[str, dict], bool] | None = None,
                 log: Callable[[str], None] | None = None):
        self.client = client
        self.toolbox = toolbox
        self.permissions = permissions
        self.system_prompt = system_prompt
        self.config = config or LoopConfig()
        self.transcript = Path(transcript) if transcript else None
        self._on_status = on_status or (lambda s: None)
        self._inbox = inbox or (lambda: [])
        self._ask = ask  # None: nobody to ask, so 'ask' means refuse
        self._log = log or (lambda s: None)
        self.messages: list[dict] = []
        self.usage = Usage()
        self.model: str | None = None
        self.steps = 0
        self.folded = 0  # how many messages the summary stands for
        self._summary_lines: list[str] = []

    # -- one turn ------------------------------------------------------------

    def run(self, prompt: str) -> str:
        """Work on ``prompt`` until the model answers without tool calls;
        that answer is returned. Raises ClientError if the endpoint fails
        for good."""
        self._on_status("processing")
        self._user(prompt)
        cutoffs = 0
        try:
            for _ in range(self.config.max_steps):
                self._drain_inbox()
                self._fit_context()
                reply = self.client.complete(self.system_prompt, self.messages, self.toolbox.specs())
                self.steps += 1
                self.usage = self.usage + reply.usage
                self.model = reply.model or self.model
                self._assistant(reply)
                if reply.tool_calls:
                    for call in reply.tool_calls:
                        self._tool(call, self._execute(call))
                    continue
                if reply.stop_reason == "length" and cutoffs < self.config.continue_on_length:
                    cutoffs += 1
                    self._user("Your reply was cut off by the output limit. Continue from where it stopped.")
                    continue
                return reply.text
            self._record({"type": "note", "text": f"stopped after {self.config.max_steps} model calls"})
            last = self.messages[-1]
            return (last.get("content") or "") if last["role"] == "assistant" else ""
        finally:
            self._on_status("idle")

    def _execute(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            return ToolResult(call.parse_error, True)
        if call.name not in self.toolbox.tools:
            return self.toolbox.call(call.name, call.arguments)  # the "unknown tool" error
        verdict = self.permissions.decide(call.name, call.arguments)
        if verdict == "ask":
            if self._ask is not None:
                self._on_status("waiting")
                try:
                    verdict = "allow" if self._ask(call.name, call.arguments) else "deny"
                finally:
                    self._on_status("processing")
            else:
                verdict = "deny"
        if verdict == "deny":
            what = call.arguments.get("command") or call.arguments.get("path") or ""
            return ToolResult(f"{call.name} {what!s} is not permitted by this worker's permissions "
                              "(permission_mode and allowed_tools). Use another approach, or report that "
                              "the task needs it.", True)
        self._log(f"{call.name} {_brief(call.arguments)}")
        return self.toolbox.call(call.name, call.arguments)

    # -- messages ------------------------------------------------------------

    def _user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._record({"type": "user", "content": text})

    def _assistant(self, reply: Reply) -> None:
        self.messages.append({"role": "assistant", "content": reply.text, "tool_calls": reply.tool_calls})
        # ``message`` has the shape copse.usage reads from Claude Code's own
        # transcripts, so a native worker's usage shows up like any other.
        self._record({"type": "assistant", "content": reply.text, "stop_reason": reply.stop_reason,
                      "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
                      "message": {"id": f"native-{self.steps}", "model": reply.model, "usage": {
                          "input_tokens": reply.usage.input_tokens, "output_tokens": reply.usage.output_tokens,
                          "cache_read_input_tokens": reply.usage.cache_read_tokens}}})

    def _tool(self, call: ToolCall, result: ToolResult) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                              "content": result.content, "is_error": result.is_error})
        self._record({"type": "tool", "tool_call_id": call.id, "name": call.name,
                      "content": result.content, "is_error": result.is_error})

    def _drain_inbox(self) -> None:
        for text in self._inbox():
            self._user(text if text.startswith(DELIVERY_PREFIX) else f"{DELIVERY_PREFIX}:\n\n{text}")

    def _record(self, entry: dict) -> None:
        if not self.transcript:
            return
        entry["ts"] = time.time()
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # -- saving the conversation (so a paused worker resumes where it was) ---

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "messages": [_to_json(m) for m in self.messages],
            "summary_lines": self._summary_lines, "folded": self.folded, "steps": self.steps,
            "usage": [self.usage.input_tokens, self.usage.output_tokens, self.usage.cache_read_tokens],
            "model": self.model,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def load(self, path: Path | str) -> bool:
        """Restore a saved conversation; False (and nothing changed) if
        ``path`` is missing or unreadable."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            messages = [_from_json(m) for m in data["messages"]]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self.messages = messages
        self._summary_lines = list(data.get("summary_lines") or [])
        self.folded = int(data.get("folded") or 0)
        self.steps = int(data.get("steps") or 0)
        u = data.get("usage") or [0, 0, 0]
        self.usage = Usage(*[int(x) for x in u])
        self.model = data.get("model")
        return True

    # -- context -------------------------------------------------------------

    def estimated_tokens(self) -> int:
        return (len(self.system_prompt) + sum(_size(m) for m in self.messages)) // 4

    def _fit_context(self) -> None:
        budget = self.config.context_tokens
        if self.estimated_tokens() <= budget:
            return
        # 1. Old tool results are the bulk, and the model has acted on them.
        keep_from = max(0, len(self.messages) - self.config.keep_recent)
        for m in self.messages[:keep_from]:
            if m["role"] == "tool" and len(m["content"]) > self.config.old_result_chars:
                m["content"] = m["content"][:self.config.old_result_chars] + "\n[trimmed]"
        if self.estimated_tokens() <= budget:
            return
        # 2. Fold the middle into a summary: the task (first user message)
        #    and the recent messages stay as they are. A summary already
        #    there (an earlier fold) is extended, not summarized again.
        start = 3 if len(self.messages) > 2 and self.messages[1].get("summary") else 1
        folded = self.messages[start:keep_from]
        if not folded:
            return
        self.folded += len(folded)
        self._summary_lines += self._summarize(folded)
        self._summary_lines = self._summary_lines[-MAX_SUMMARY_LINES:]
        summary = "\n".join([
            "[Earlier work in this session, summarized by copse to save context]",
            *self._summary_lines,
            "[End of summary. Files you changed are still changed on disk; re-read anything you need exactly.]",
        ])
        self.messages[1:keep_from] = [
            {"role": "user", "content": summary, "summary": True},
            {"role": "assistant", "content": "Understood; continuing from that state.", "tool_calls": []},
        ]
        # Recent messages must not start with a tool result whose call was
        # folded away: drop leading orphans.
        while len(self.messages) > 3 and self.messages[3]["role"] == "tool":
            del self.messages[3]
        self._record({"type": "note", "text": f"context folded: {len(folded)} messages summarized"})

    @staticmethod
    def _summarize(messages: list[dict]) -> list[str]:
        lines = []
        for m in messages:
            if m["role"] == "assistant":
                if m.get("content"):
                    lines.append(f"- you said: {_short(m['content'], 300)}")
                for c in m.get("tool_calls") or []:
                    lines.append(f"- you ran {c.name} {_brief(c.arguments)}")
            elif m["role"] == "tool":
                status = "error" if m.get("is_error") else "ok"
                lines.append(f"  -> {status}: {_short(m['content'], 160)}")
            elif m["role"] == "user":
                lines.append(f"- message: {_short(m['content'], 300)}")
        return lines

def _to_json(m: dict) -> dict:
    out = dict(m)
    if "tool_calls" in out:
        out["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in out["tool_calls"]]
    return out


def _from_json(m: dict) -> dict:
    out = dict(m)
    if "tool_calls" in out:
        out["tool_calls"] = [ToolCall(c["id"], c["name"], c.get("arguments") or {}) for c in out["tool_calls"]]
    return out


def _size(m: dict) -> int:
    n = len(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += len(c.name) + len(json.dumps(c.arguments))
    return n


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _brief(args: dict) -> str:
    for key in ("command", "path", "pattern"):
        if key in args:
            return _short(str(args[key]), 120)
    return _short(json.dumps(args), 120)
