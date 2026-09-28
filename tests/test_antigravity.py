import json
import os
import subprocess
import sys
import time

import pytest

from conftest import sh
from copse import agents, antigravity, autopilot, workspaces
from copse.db import Agent
from copse.profiles import load_profile
from copse.providers import Antigravity, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add_agent(db, ws, agent_id="g1", mode="interactive", status="idle", profile="supervisor"):
    db.add_agent(Agent(agent_id, ws.id, profile, "antigravity", None, mode, status, "@0", None, time.time()))


def test_command_sets_up_the_checkout(db, ws, repo, monkeypatch):
    monkeypatch.setenv("COPSE_AGY_BIN", "/bin/agy")
    argv = Antigravity().command(LaunchContext("g1", load_profile("developer"), None, cwd=str(repo)))
    assert argv == ["/bin/agy", "--mode", "accept-edits"]
    agents_dir = repo / ".agents"
    server = json.loads((agents_dir / "mcp_config.json").read_text())["mcpServers"]["copse"]
    assert server["args"][-1] == "mcp" and server["tools"]["get_progress"] == {"eager": True}
    hooks = json.loads((agents_dir / "hooks.json").read_text())["copse"]
    assert "_hook agy-stop" in hooks["Stop"][0]["command"]
    assert "COPSE_AGENT_ID" not in hooks["Stop"][0]["command"]  # shared by every agent here
    assert "call_mcp_tool" in (agents_dir / "rules" / "copse.md").read_text()
    # Kept out of git, so the checkout stays clean.
    assert sh("git status --porcelain", repo) == ""


def test_resume_uses_the_conversation(db, ws, repo):
    argv = Antigravity().command(LaunchContext("g1", load_profile("developer"), None, resume="abc", cwd=str(repo)))
    assert argv[-2:] == ["--conversation", "abc"]


def test_existing_config_is_kept_and_tracked_files_are_left_alone(repo):
    (repo / ".agents").mkdir()
    (repo / ".agents" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}))
    antigravity.install(str(repo))
    servers = json.loads((repo / ".agents" / "mcp_config.json").read_text())["mcpServers"]
    assert set(servers) == {"fs", "copse"}
    # A file that was already there isn't hidden from git.
    assert ".agents/mcp_config.json" in sh("git status --porcelain --untracked-files=all", repo)
    (repo / ".agents" / "hooks.json").write_text("{}")
    sh("git add -f .agents/hooks.json && git commit -qm cfg", repo)
    with pytest.raises(antigravity.AntigravityError, match="committed"):
        antigravity.install(str(repo))


def test_hook_finds_its_agent_from_the_agy_process(db, ws, monkeypatch):
    add_agent(db, ws, status="processing")
    monkeypatch.delenv("COPSE_AGENT_ID", raising=False)
    parent = os.getppid()
    monkeypatch.setattr(antigravity, "_process_env", lambda pid: {"COPSE_AGENT_ID": "g1"} if pid == parent else {})
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "false", None)])
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    assert json.loads(antigravity.hook_main(db, "agy-stop", "{}"))["decision"] == "continue"
    monkeypatch.setattr(antigravity, "_process_env", lambda pid: {})
    monkeypatch.setattr(antigravity, "_parent", lambda pid: None)
    assert antigravity.hook_main(db, "agy-stop", "{}") == "{}"


def test_process_env_reads_another_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                             env={**os.environ, "COPSE_AGENT_ID": "abc123"})
    try:
        time.sleep(0.3)
        assert antigravity._process_env(child.pid).get("COPSE_AGENT_ID") == "abc123"
    finally:
        child.kill()


def test_warmup_then_the_task(db, ws, monkeypatch):
    monkeypatch.setattr(agents.tmux, "ensure_session", lambda *a: None)
    monkeypatch.setattr(agents.tmux, "new_window", lambda *a, **k: "@9")
    monkeypatch.setattr(agents.tmux, "apply_theme", lambda *a: None)
    monkeypatch.setattr(Antigravity, "command", lambda self, ctx: ["agy"])
    monkeypatch.setattr(Antigravity, "after_launch", lambda self, t: None)
    a = agents.spawn(db, ws, "developer", prompt="fix it", provider_name="antigravity", mode="assign")
    first, second = db.pop_pending(a.id), db.pop_pending(a.id)
    assert first.body.startswith("When you run under copse") and "call_mcp_tool" in first.body
    assert "You are a developer agent" in first.body and first.body.endswith(antigravity.WARMUP_END)
    assert second.body.startswith("fix it")


def test_queued_messages_arrive_as_a_new_turn(db, ws, monkeypatch):
    flushed = []
    monkeypatch.setattr(antigravity, "_flush_soon", flushed.append)
    add_agent(db, ws, status="processing")
    db.enqueue("g1", "also add tests", None)
    out = antigravity.handle_hook(db, "g1", "agy-stop", {"conversationId": "c1", "terminationReason": "NO_TOOL_CALL"})
    assert out is None and flushed == ["g1"]
    assert db.get_agent("g1").status == "idle" and db.get_agent("g1").session_ref == "c1"
    assert db.pending_count("g1") == 1   # typed in by the flush, as a new turn


def test_report_reminder_only_once_per_turn(db, ws):
    add_agent(db, ws, mode="assign", status="processing", profile="developer")
    out = antigravity.handle_hook(db, "g1", "agy-stop", {})
    assert out and "report_result" in out["reason"]
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is None
    # A new turn resets it.
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert antigravity.handle_hook(db, "g1", "agy-stop", {}) is not None


def test_new_turn_is_the_user_unless_copse_just_delivered(db, ws):
    add_agent(db, ws)
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "true", None)])
    autopilot.need_user(db, "g1", "Which DB?")
    db.enqueue("g1", "Assigned task finished.", "w1")
    db.pop_pending("g1")
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert db.get_autopilot("g1").state == "blocked"
    assert db.get_agent("g1").status == "processing"
    with db.tx() as c:
        c.execute("UPDATE inbox SET delivered_at = delivered_at - 60")
    antigravity.handle_hook(db, "g1", "agy-pre-invocation", {"invocationNum": 0})
    assert db.get_autopilot("g1").state == "running"


def test_usage_limit_error_blocks_autopilot(db, ws):
    add_agent(db, ws, status="processing")
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "true", None)])
    antigravity.handle_hook(db, "g1", "agy-stop", {"terminationReason": "error",
                                                   "error": "429 RESOURCE_EXHAUSTED: quota"})
    assert db.get_autopilot("g1").state == "blocked"


def test_autopilot_keeps_an_antigravity_supervisor_going(db, ws, monkeypatch):
    add_agent(db, ws, status="processing")
    db.add_autopilot("g1")
    autopilot.set_goal(db, "g1", "Goal", [("M", "false", None)])
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    out = antigravity.handle_hook(db, "g1", "agy-stop", {})
    assert out["decision"] == "continue" and "[copse autopilot]" in out["reason"]


def test_screen_states():
    p = Antigravity()
    assert p.screen_state("Requesting permission for:\n  echo hi\nRun this command?\nesc to cancel") == "waiting"
    assert p.screen_state("> \n───\nesc to cancel      Gemini") == "busy"
    assert p.screen_state("> \n───\n? for shortcuts      Gemini") == "idle"


