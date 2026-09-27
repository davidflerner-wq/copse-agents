import shutil
import time

import pytest

from copse import agents, tmux, workspaces
from copse.db import Agent
from copse.providers import ClaudeCode, LaunchContext
from copse.profiles import load_profile


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
    assert '"COPSE_AGENT_ID": "abc"' in argv[argv.index("--mcp-config") + 1]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_shell_agent_in_tmux_end_to_end(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        assert agents.is_alive(a)
        assert agents.send_message(db, a.id, "echo copse-says-hi-$COPSE_AGENT_ID") == "delivered"
        deadline = time.time() + 5
        while time.time() < deadline and f"copse-says-hi-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"copse-says-hi-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


def test_developer_may_run_tests_and_builds_but_not_everything():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), None))
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__copse" in allowed
    assert "Bash(pytest:*)" in allowed and "Bash(npm run:*)" in allowed
    assert "Bash(git push:*)" not in allowed
    assert not any(t in ("Bash", "Bash(*)") for t in allowed)


CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✶ Thinking… (12s)\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n"
CLAUDE_PROMPT = " Bash command\n   pytest\n This command requires approval\n\n Do you want to proceed?\n ❯ 1. Yes\n   4. No\n\n Esc to cancel\n"


@pytest.mark.parametrize("screen,want", [(CLAUDE_IDLE, "idle"), (CLAUDE_BUSY, "busy"), (CLAUDE_PROMPT, "waiting"), ("", None)])
def test_claude_screen_state(screen, want):
    assert ClaudeCode().screen_state(screen) == want


def test_reconcile_recovers_from_interrupted_turn(db, ws, monkeypatch):
    # Esc-interrupted turns run no Stop hook: status says waiting, screen says idle.
    fake_agent(db, ws, status="waiting")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    a = agents.reconcile(db, db.get_agent("a1"), gap=0)
    assert a.status == "idle" == db.get_agent("a1").status


def test_reconcile_keeps_hook_status_when_screen_is_unclear(db, ws, monkeypatch):
    fake_agent(db, ws, status="processing")
    screens = iter([CLAUDE_IDLE, CLAUDE_BUSY])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    assert agents.reconcile(db, db.get_agent("a1"), gap=0).status == "processing"


def test_handoff_wait_is_bounded_and_detaches(db, ws, monkeypatch):
    from copse import mcp_server

    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="handoff", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(agents, "kill", lambda db, aid: db.delete_agent(aid))

    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "still running" in out and "wait_for_worker" in out
    assert db.get_agent("w1").mode == "handoff_detached"

    # Finishing later forwards the result to the supervisor's inbox...
    agents.report_result(db, "w1", "done later")
    assert db.pending_count("boss") == 1
    # ...and if the supervisor collects it directly, the duplicate is dropped.
    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "done later" in out
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1") is None  # handoff worker closed after collection


def test_result_arriving_during_detach_is_not_lost(db, ws, monkeypatch):
    fake_agent(db, ws, mode="handoff", agent_id="w1")
    db.set_result("w1", "just in time")
    assert agents.detach(db, "w1") == "just in time"


def test_wait_for_worker_leaves_assign_workers_running(db, ws, monkeypatch):
    from copse import mcp_server

    fake_agent(db, ws, mode="assign", agent_id="w2")
    db.set_result("w2", "ok")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    killed = []
    monkeypatch.setattr(agents, "kill", lambda db, aid: killed.append(aid))
    assert "ok" in mcp_server._await_worker(db, "w2", wait_seconds=0)
    assert killed == []


def test_codex_command_preapproves_only_copse_tools(monkeypatch):
    from copse.providers import Codex

    monkeypatch.setenv("COPSE_CODEX_BIN", "/opt/codex")
    argv = Codex().command(LaunchContext("abc", load_profile("developer"), "do it"))
    assert argv[0] == "/opt/codex"
    assert 'mcp_servers.copse.default_tools_approval_mode="approve"' in argv
    assert any('COPSE_AGENT_ID = "abc"' in a for a in argv)
    assert argv[-1].endswith("do it")  # profile prompt leads the first message
    assert not any("dangerously" in a or "full-auto" in a for a in argv)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_watch_pane_shares_the_window_and_messages_reach_the_agent(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    try:
        assert a.tmux_window.startswith("%")  # a pane, not a window
        panes = tmux._tmux("list-panes", "-t", a.tmux_window, "-F", "#{pane_id}").stdout.split()
        assert len(panes) == 2 and a.tmux_window in panes
        # Even with the dashboard pane focused, messages go to the agent's pane.
        other = next(p for p in panes if p != a.tmux_window)
        tmux._tmux("select-pane", "-t", other)
        agents.send_message(db, a.id, "echo reached-$COPSE_AGENT_ID")
        deadline = time.time() + 5
        while time.time() < deadline and f"reached-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"reached-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)
