"""Delivering messages through Claude Code's own inbox.

Every Claude Code session listens on a Unix socket for messages from other
sessions, and tells its child processes where it is (``CLAUDE_CODE_MESSAGING_SOCKET``
and ``CLAUDE_CODE_MESSAGING_TOKEN``). copse's hooks are such children, so the
SessionStart hook records the socket for the agent, and from then on copse
sends messages to it there instead of typing them into the agent's pane.

What that changes: a message no longer waits for the agent to go idle. An
idle agent starts a new turn with it; a busy one gets it between tool calls
and acts on it once the current step is done. And nothing depends on
reading the screen to know when typing is safe.

The wire format (a JSON line each): an auth frame, then the message::

    {"type": "auth", "token": "<token>"}
    {"type": "user", "message": {"content": "<text>"}, "from": "copse",
     "priority": "next", "uuid": "<uuid>"}

Claude Code presents the text as a message from a peer named ``from``. It
respects the session's ``crossSessionInbound`` setting: ``accept`` (the
default) delivers, ``hold`` asks the person first, ``refuse`` drops. Typing
into the pane remains the fallback whenever the socket isn't there.
"""

from __future__ import annotations

import json
import os
import socket
import uuid

from copse.db import DB, Agent

SOCKET_VAR = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_VAR = "CLAUDE_CODE_MESSAGING_TOKEN"


def record_from_environment(db: DB, agent_id: str) -> bool:
    """Called from a hook running inside the agent's Claude Code process:
    remember its inbox. Returns whether one was found."""
    path, token = os.environ.get(SOCKET_VAR), os.environ.get(TOKEN_VAR)
    if not path or not token:
        return False
    db.update_agent(agent_id, inbox_socket=path, inbox_token=token)
    return True


def usable(agent: Agent) -> bool:
    return bool(agent.inbox_socket) and bool(agent.inbox_token) and os.path.exists(agent.inbox_socket)


def send(agent: Agent, text: str, sender: str = "copse", timeout: float = 3.0) -> bool:
    """Deliver ``text`` to the agent's inbox. False if it couldn't be."""
    if not usable(agent):
        return False
    frames = [
        {"type": "auth", "token": agent.inbox_token},
        {"type": "user", "message": {"content": text}, "from": sender,
         "priority": "next", "uuid": str(uuid.uuid4())},
    ]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(agent.inbox_socket)
        s.sendall("".join(json.dumps(f) + "\n" for f in frames).encode())
        s.close()
    except OSError:
        return False
    return True
