"""copse's own agent loop: a worker that talks to a chat endpoint directly.

The wrapped CLIs (Claude Code, Codex, Antigravity) bring their own harness,
and copse learns their state from hooks or the screen. This package is the
harness copse runs itself, for models reachable over an OpenAI-compatible
chat-completions API or the Anthropic Messages API: Ollama, llama.cpp, LM
Studio, OpenRouter, Z.ai, DeepSeek, Anthropic, ... The loop knows exactly
when it's working, idle, or refused a tool, and takes queued messages
between turns, so nothing has to be inferred.

- ``client``: the two wire formats behind one ``Client.complete`` call.
- ``tools``: the worker's tools (Read, Write, Edit, Glob, Grep, Bash).
- ``permissions``: ``permission_mode`` and ``allowed_tools`` patterns, the
  same shapes a profile gives Claude Code.
- ``loop``: the turn loop, context compaction and the transcript log.
"""

from copse.native.client import Client, ClientError, Endpoint, Reply, ToolCall, ToolSpec, Usage
from copse.native.loop import LoopConfig, NativeAgent
from copse.native.permissions import Permissions
from copse.native.tools import Tool, Toolbox, ToolResult, core_tools

__all__ = [
    "Client", "ClientError", "Endpoint", "Reply", "ToolCall", "ToolSpec", "Usage",
    "LoopConfig", "NativeAgent", "Permissions",
    "Tool", "Toolbox", "ToolResult", "core_tools",
]
