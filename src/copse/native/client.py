"""One ``complete`` call over either chat wire format.

Messages are kept in copse's own shape and converted per request, so the
loop never sees the difference between backends:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall, ...]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "...", "is_error": bool}

The system prompt is passed separately (OpenAI wants it as a message,
Anthropic as a top-level field). Only the standard library is used: a
worker's dependencies should stay copse's own.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments object


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Set when the model's arguments weren't a JSON object: the loop hands
    # this back as the tool's error so the model can try again.
    parse_error: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens,
                     self.cache_read_tokens + other.cache_read_tokens)


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # 'stop' | 'tool_calls' | 'length' | other backend value
    usage: Usage = field(default_factory=Usage)
    model: str | None = None


@dataclass
class Endpoint:
    """Where and how to talk to the model."""

    base_url: str
    model: str
    api: str = "openai"          # 'openai' (chat completions) | 'anthropic' (messages)
    api_key: str | None = None
    max_tokens: int = 8192
    timeout: float = 600.0       # a slow local model can take minutes per reply
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)

    def url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api == "anthropic":
            return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        return base + "/chat/completions"


class ClientError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Client:
    def __init__(self, endpoint: Endpoint, sleep=time.sleep):
        self.endpoint = endpoint
        self._sleep = sleep

    # -- the one public call ------------------------------------------------

    def complete(self, system: str | None, messages: list[dict], tools: list[ToolSpec]) -> Reply:
        if self.endpoint.api == "anthropic":
            payload = self._anthropic_request(system, messages, tools)
            return self._parse_anthropic(self._post(payload))
        payload = self._openai_request(system, messages, tools)
        return self._parse_openai(self._post(payload))

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "copse-native"}
        key = self.endpoint.api_key
        if self.endpoint.api == "anthropic":
            h["anthropic-version"] = ANTHROPIC_VERSION
            if key:
                h["x-api-key"] = key
                h["Authorization"] = f"Bearer {key}"  # gateways that want it this way
        elif key:
            h["Authorization"] = f"Bearer {key}"
        h.update(self.endpoint.headers)
        return h

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        last: ClientError | None = None
        for attempt in range(self.endpoint.retries + 1):
            req = urllib.request.Request(self.endpoint.url(), data=data, headers=self._headers(),
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.endpoint.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except ValueError as e:
                    raise ClientError(f"the endpoint's reply wasn't JSON: {e}", body=body[:2000]) from None
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                last = ClientError(f"HTTP {e.code} from {self.endpoint.url()}: {_error_text(body)}",
                                   status=e.code, body=body[:2000])
                if e.code not in RETRY_STATUSES:
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = ClientError(f"couldn't reach {self.endpoint.url()}: {e}")
            if attempt < self.endpoint.retries:
                self._sleep(min(2 ** attempt, 20))
        assert last is not None
        raise last

    # -- OpenAI chat completions --------------------------------------------

    def _openai_request(self, system: str | None, messages: list[dict], tools: list[ToolSpec]) -> dict:
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or None}
                calls = m.get("tool_calls") or []
                if calls:
                    entry["tool_calls"] = [{
                        "id": c.id, "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    } for c in calls]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": role, "content": m["content"]})
        payload: dict = {"model": self.endpoint.model, "messages": out,
                         "max_tokens": self.endpoint.max_tokens, "stream": False}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }} for t in tools]
        return payload

    @staticmethod
    def _parse_openai(data: dict) -> Reply:
        choices = data.get("choices") or []
        if not choices:
            raise ClientError(f"no choices in reply: {_error_text(json.dumps(data))}")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        calls = []
        for i, c in enumerate(message.get("tool_calls") or []):
            fn = c.get("function") or {}
            calls.append(_tool_call(c.get("id") or f"call_{i}", fn.get("name") or "", fn.get("arguments")))
        finish = choice.get("finish_reason") or ("tool_calls" if calls else "stop")
        stop = {"stop": "stop", "tool_calls": "tool_calls", "length": "length"}.get(finish, finish)
        if calls and stop == "stop":
            stop = "tool_calls"
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                      int(details.get("cached_tokens") or 0))
        return Reply(text, calls, stop, usage, data.get("model"))

    # -- Anthropic messages ---------------------------------------------------

    def _anthropic_request(self, system: str | None, messages: list[dict], tools: list[ToolSpec]) -> dict:
        out: list[dict] = []
        for m in messages:
            role = m["role"]
            if role == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if m.get("is_error"):
                    block["is_error"] = True
                # Consecutive tool results share one user message, as the API requires.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][-1].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append({"type": "text", "text": m["content"]})
                else:
                    out.append({"role": "user", "content": m["content"]})
        payload: dict = {"model": self.endpoint.model, "messages": out,
                         "max_tokens": self.endpoint.max_tokens}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": t.name, "description": t.description,
                                 "input_schema": t.parameters} for t in tools]
        return payload

    @staticmethod
    def _parse_anthropic(data: dict) -> Reply:
        if data.get("type") == "error":
            raise ClientError(f"error from the endpoint: {_error_text(json.dumps(data))}")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, block in enumerate(data.get("content") or []):
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text") or "")
            elif kind == "tool_use":
                calls.append(_tool_call(block.get("id") or f"toolu_{i}", block.get("name") or "",
                                        block.get("input")))
        reason = data.get("stop_reason") or ("tool_use" if calls else "end_turn")
        stop = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length",
                "stop_sequence": "stop"}.get(reason, reason)
        if calls and stop == "stop":
            stop = "tool_calls"
        u = data.get("usage") or {}
        usage = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                      int(u.get("cache_read_input_tokens") or 0))
        return Reply("".join(text_parts), calls, stop, usage, data.get("model"))


def _tool_call(call_id: str, name: str, arguments) -> ToolCall:
    """A ToolCall from whatever the model sent as arguments: a JSON string
    (OpenAI), an object (Anthropic), or something broken."""
    if arguments is None or arguments == "":
        return ToolCall(call_id, name, {})
    if isinstance(arguments, dict):
        return ToolCall(call_id, name, arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError as e:
            return ToolCall(call_id, name, {}, parse_error=f"arguments were not valid JSON ({e}): {arguments[:500]}")
        if isinstance(parsed, dict):
            return ToolCall(call_id, name, parsed)
        return ToolCall(call_id, name, {}, parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}")
    return ToolCall(call_id, name, {}, parse_error=f"arguments must be a JSON object, got {type(arguments).__name__}")


def _error_text(body: str) -> str:
    """The message inside an error body, if it has the usual shape."""
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300].strip() or "(empty body)"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:300]
    if isinstance(err, str):
        return err[:300]
    return body[:300].strip() or "(empty body)"
