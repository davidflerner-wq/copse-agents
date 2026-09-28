"""Streaming replies: SSE for both wire formats, the fallback when the
endpoint refuses a stream, and the loop end to end."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from copse.native import (Client, Endpoint, NativeAgent, Permissions, Toolbox, ToolSpec, core_tools)


class Sse:
    """A scripted reply sent as text/event-stream: a list of (event, data) pairs."""

    def __init__(self, events, done=True):
        self.events = events
        self.done = done

    def body(self) -> bytes:
        out = ""
        for event, data in self.events:
            out += (f"event: {event}\n" if event else "") + f"data: {json.dumps(data)}\n\n"
        if self.done:
            out += "data: [DONE]\n\n"
        return out.encode()


class FakeEndpoint:
    """Serves scripted replies in order: an Sse, a dict (JSON, 200), or an
    int status (an error). Records every request body."""

    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n)))
                reply = outer.replies.pop(0) if outer.replies else 500
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": f"scripted {reply}"}}).encode())
                    return
                ctype = "application/json"
                if isinstance(reply, Sse):
                    body, ctype = reply.body(), "text/event-stream"
                else:
                    body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    ep = FakeEndpoint()
    yield ep
    ep.close()


def make_client(fake, api="openai") -> Client:
    ep = Endpoint(fake.base_url + ("/v1" if api == "openai" else ""), "fake-model", api=api, retries=0)
    return Client(ep, sleep=lambda s: None)


def oa_chunk(delta=None, finish=None, usage=None):
    c = {"model": "fake-1", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return (None, c)


def openai_stream() -> Sse:
    return Sse([
        oa_chunk({"role": "assistant", "content": "Let me "}),
        oa_chunk({"content": "look."}),
        oa_chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                  "function": {"name": "Bash", "arguments": ""}}]}),
        oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"command": '}}]}),
        oa_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"echo hi"}'}}]}),
        oa_chunk(finish="tool_calls"),
        (None, {"model": "fake-1", "choices": [],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7,
                          "prompt_tokens_details": {"cached_tokens": 3}}}),
    ])


def anthropic_stream(text_parts=("Let me ", "look."), stop="tool_use") -> Sse:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "model": "fake-a", "usage": {"input_tokens": 12, "output_tokens": 1,
                                         "cache_read_input_tokens": 3}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
    ]
    for t in text_parts:
        events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": t}}))
    events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if stop == "tool_use":
        events += [
            ("content_block_start", {"type": "content_block_start", "index": 1,
                                     "content_block": {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta", "partial_json": '{"command": '}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta", "partial_json": '"echo hi"}'}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ]
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop},
                                  "usage": {"output_tokens": 7}}),
               ("message_stop", {"type": "message_stop"})]
    return Sse(events, done=False)


BASH = ToolSpec("Bash", "run", {"type": "object"})


def test_openai_stream_assembles_text_and_tool_call(fake):
    fake.replies = [openai_stream()]
    seen: list[str] = []
    reply = make_client(fake).complete("sys", [{"role": "user", "content": "go"}], [BASH], on_text=seen.append)
    assert fake.requests[0]["stream"] is True
    assert seen == ["Let me ", "look."]
    assert reply.text == "Let me look."
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [("c1", "Bash", {"command": "echo hi"})]
    assert reply.stop_reason == "tool_calls"
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cache_read_tokens) == (12, 7, 3)
    assert reply.model == "fake-1"


def test_anthropic_stream_assembles_text_and_input_json(fake):
    fake.replies = [anthropic_stream()]
    seen: list[str] = []
    reply = make_client(fake, "anthropic").complete("sys", [{"role": "user", "content": "go"}], [BASH],
                                                    on_text=seen.append)
    assert fake.requests[0]["stream"] is True
    assert seen == ["Let me ", "look."]
    assert reply.text == "Let me look."
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [("tu1", "Bash", {"command": "echo hi"})]
    assert reply.stop_reason == "tool_calls"
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cache_read_tokens) == (12, 7, 3)
    assert reply.model == "fake-a"


def test_stream_keeps_text_tool_call_recovery(fake):
    block = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
    fake.replies = [Sse([oa_chunk({"content": "ok "}), oa_chunk({"content": block}), oa_chunk(finish="stop")])]
    reply = make_client(fake).complete(None, [{"role": "user", "content": "go"}], [BASH], on_text=lambda t: None)
    assert reply.text == "ok"
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("Bash", {"command": "ls"})]
    assert reply.stop_reason == "tool_calls"


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_rejected_stream_falls_back_to_plain_request(fake, api):
    plain = ({"model": "m", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 2}} if api == "openai" else
             {"type": "message", "model": "m", "content": [{"type": "text", "text": "hello"}],
              "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}})
    fake.replies = [400, plain]
    seen: list[str] = []
    reply = make_client(fake, api).complete(None, [{"role": "user", "content": "go"}], [], on_text=seen.append)
    assert [r.get("stream") for r in fake.requests] == [True, False]
    assert "stream_options" not in fake.requests[1]
    assert reply.text == "hello" and reply.usage.output_tokens == 2


def test_plain_json_answer_to_a_stream_request_still_works(fake):
    fake.replies = [{"model": "m", "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}]}]
    seen: list[str] = []
    reply = make_client(fake).complete(None, [{"role": "user", "content": "go"}], [], on_text=seen.append)
    assert reply.text == "hi there" and seen == ["hi there"]


def test_without_on_text_nothing_streams(fake):
    fake.replies = [{"model": "m", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}]
    make_client(fake).complete(None, [{"role": "user", "content": "go"}], [])
    assert fake.requests[0]["stream"] is False


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_loop_end_to_end_with_streaming(fake, tmp_path: Path, api):
    if api == "openai":
        final = Sse([oa_chunk({"content": "All "}), oa_chunk({"content": "done."}), oa_chunk(finish="stop"),
                     (None, {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}})])
        first = openai_stream()
    else:
        final = anthropic_stream(("All ", "done."), stop="end_turn")
        first = anthropic_stream()
    fake.replies = [first, final]
    seen: list[str] = []
    box = Toolbox().add(*core_tools(str(tmp_path), bash_timeout=5))
    loop = NativeAgent(make_client(fake, api), box, Permissions("acceptEdits", ["Bash(echo:*)"]),
                       "You are a test worker.", on_text=seen.append)
    answer = loop.run("say hi")
    assert answer == "All done."
    assert seen == ["Let me ", "look.", "All ", "done."]
    assert all(r["stream"] is True for r in fake.requests)
    tool_msgs = [m for m in loop.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and "hi" in tool_msgs[0]["content"] and not tool_msgs[0]["is_error"]
    assert loop.usage.input_tokens == 24
