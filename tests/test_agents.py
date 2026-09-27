import shutil
import time

import pytest

from grove import agents, tmux, workspaces
from grove.db import Agent
from grove.providers import ClaudeCode, LaunchContext
from grove.profiles import load_profile


def fake_agent(db, ws, status="processing", mode="interactive", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def test_stop_hook_delivers_queued_message(db, ws):
    fake_agent(db, ws)
    db.enqueue("a1", "please also add tests", None)
    out = agents.handle_hook(db, "a1", "stop", {})
    assert out == {"decision": "block", "reason": "please also add tests"}
    assert db.get_agent("a1").status == "processing"
    assert agents.handle_hook(db, "a1", "stop", {}) is None
    assert db.get_agent("a1").status == "idle"


def test_stop_hook_nudges_worker_to_report_once(db, ws):
    fake_agent(db, ws, mode="handoff")
    out = agents.handle_hook(db, "a1", "stop", {})
    assert out and "report_result" in out["reason"]
    # Claude Code sets stop_hook_active on the follow-up stop; don't loop.
    assert agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True}) is None


def test_prompt_and_notification_hooks(db, ws):
    fake_agent(db, ws, status="idle")
    agents.handle_hook(db, "a1", "prompt-submit", {})
    assert db.get_agent("a1").status == "processing"
    agents.handle_hook(db, "a1", "notification", {"message": "Claude needs your permission to use Bash"})
    assert db.get_agent("a1").status == "waiting"


def test_claim_idle_is_exclusive(db, ws):
    fake_agent(db, ws, status="idle")
    assert db.claim_idle("a1") is True
    assert db.claim_idle("a1") is False


def test_report_result_forwards_to_parent_on_assign(db, ws, monkeypatch):
    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="assign", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert "sent" in agents.report_result(db, "w1", "done: added login")
    assert db.get_agent("w1").result == "done: added login"
    # Boss is busy, so the result waits in its inbox for the next Stop hook.
    msg = db.pop_pending("boss")
    assert msg and "done: added login" in msg.body and "w1" in msg.body


def test_claude_command_wires_hooks_mcp_and_profile():
    ctx = LaunchContext("abc", load_profile("developer"), "do the thing")
    argv = ClaudeCode().command(ctx)
    assert argv[0] == "claude" and argv[-1] == "do the thing"
    settings = argv[argv.index("--settings") + 1]
    assert "_hook" in settings and "Stop" in settings
    assert '"GROVE_AGENT_ID": "abc"' in argv[argv.index("--mcp-config") + 1]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_shell_agent_in_tmux_end_to_end(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        assert agents.is_alive(a)
        assert agents.send_message(db, a.id, "echo grove-says-hi-$GROVE_AGENT_ID") == "delivered"
        deadline = time.time() + 5
        while time.time() < deadline and f"grove-says-hi-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"grove-says-hi-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


def test_developer_may_run_tests_and_builds_but_not_everything():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), None))
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__grove" in allowed
    assert "Bash(pytest:*)" in allowed and "Bash(npm run:*)" in allowed
    assert "Bash(git push:*)" not in allowed
    assert not any(t in ("Bash", "Bash(*)") for t in allowed)
