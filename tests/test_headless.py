"""Headless workers: `claude -p` turn by turn, under copse's runner."""

import json
import shutil
import sys
import time

import pytest

from copse import agents, tmux, workspaces
from copse.db import Agent

FAKE_CLAUDE = """#!/bin/sh
# Stands in for `claude -p`: logs its argv, reports like Claude Code's hooks.
"{py}" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" >> "{log}"
sid=""
prev=""
for a in "$@"; do
  case "$prev" in --session-id|--resume) sid="$a";; esac
  prev="$a"
done
for a in "$@"; do last="$a"; done
echo "{{\\"session_id\\": \\"$sid\\"}}" | "{py}" -m copse _hook session-start
echo "{{\\"session_id\\": \\"$sid\\"}}" | "{py}" -m copse _hook prompt-submit
case "$last" in
  *FAIL*) echo "API Error: overloaded" >&2; exit 3;;
esac
echo "did: $last"
echo "{{\\"session_id\\": \\"$sid\\"}}" | "{py}" -m copse _hook stop > /dev/null
"""


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    log = tmp_path / "claude-argv.log"
    script = tmp_path / "fake-claude"
    script.write_text(FAKE_CLAUDE.format(py=sys.executable, log=log))
    script.chmod(0o755)
    monkeypatch.setenv("COPSE_CLAUDE_BIN", str(script))

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


@pytest.fixture
def cheap_profile(repo):
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True)
    (d / "cheap.md").write_text(
        "---\nname: cheap\ndescription: cheap worker\nprovider: claude\n"
        "headless: true  # claude -p\nstrict_mcp: true\nsetting_sources: project,local\n"
        "effort: low\npermission_mode: acceptEdits\n---\nYou are cheap.\n"
    )
    return "cheap"


def headless_agent(db, ws, *, status="idle", mode="assign", agent_id="h1", window="@0"):
    a = Agent(agent_id, ws.id, "developer", "claude", None, mode, status, window, None,
              time.time(), headless=1)
    db.add_agent(a)
    return a


def test_runner_starts_a_session_then_resumes_it(db, ws, fake_claude, monkeypatch):
    headless_agent(db, ws, status="processing")
    monkeypatch.setenv("COPSE_AGENT_ID", "h1")
    db.enqueue("h1", "first task", None)
    assert agents.run_headless(db, "h1", exit_when_idle=True) == 0
    sid = db.get_agent("h1").session_ref
    db.enqueue("h1", "and the tests", None)
    assert agents.run_headless(db, "h1", resume=sid, exit_when_idle=True) == 0

    first, second = fake_claude()
    assert first[0] == "-p" and first[-1] == "first task"
    sid = first[first.index("--session-id") + 1]
    assert second[-3:] == ["--resume", sid, "and the tests"]
    assert "--session-id" not in second
    a = db.get_agent("h1")
    assert a.session_ref == sid and a.status == "idle"


def test_runner_stops_when_claude_fails(db, ws, fake_claude, monkeypatch, capfd):
    headless_agent(db, ws)
    monkeypatch.setenv("COPSE_AGENT_ID", "h1")
    db.enqueue("h1", "please FAIL", None)
    db.enqueue("h1", "never run", None)
    assert agents.run_headless(db, "h1", exit_when_idle=True) == 3
    assert len(fake_claude()) == 1
    assert "exited with code 3" in capfd.readouterr().out


def test_messages_to_a_headless_worker_go_through_its_inbox(db, ws, monkeypatch):
    headless_agent(db, ws, status="idle")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)

    def no_typing(*a, **k):
        raise AssertionError("nothing is typed into a headless worker's pane")

    monkeypatch.setattr(tmux, "paste", no_typing)
    monkeypatch.setattr(tmux, "capture", no_typing)
    # Idle: the runner starts the next turn with it.
    assert agents.send_message(db, "h1", "one more thing") == "delivered"
    assert db.pending_count("h1") == 1
    assert agents.flush(db, "h1") is False and db.pending_count("h1") == 1
    db.pop_pending("h1")
    # Mid-turn: it waits, and the Stop hook hands it over like for any Claude.
    db.set_status("h1", "processing")
    assert agents.send_message(db, "h1", "also this") == "queued"
    out = agents.handle_hook(db, "h1", "stop", {})
    assert out and out["decision"] == "block" and "also this" in out["reason"]


def test_session_start_does_not_schedule_typing_for_headless(db, ws, monkeypatch):
    headless_agent(db, ws, status="processing")
    db.enqueue("h1", "queued", None)
    started = []
    monkeypatch.setattr(agents.subprocess, "Popen", lambda *a, **k: started.append(a))
    agents.handle_hook(db, "h1", "session-start", {"session_id": "s1"})
    assert started == []
    assert db.get_agent("h1").session_ref == "s1"


def test_resumed_headless_worker_gets_a_turn_to_continue(db, ws, monkeypatch):
    a = headless_agent(db, ws, status="paused", window="")
    opened = []
    monkeypatch.setattr(agents, "_open_window",
                        lambda db, agent, ws, name, argv, watch: opened.append(argv) or "@9")
    agents._launch(db, a, ws, prompt=None, resume="sid-1", watch_pane=False)
    assert opened[0][-4:] == ["_headless", "h1", "--resume", "sid-1"]
    msg = db.pop_pending("h1")
    assert msg and msg.body == agents.HEADLESS_CONTINUE
    assert db.get_agent("h1").status == "processing"


def test_headless_worker_with_finish_line_skips_goal_command(db, ws, cheap_profile, monkeypatch):
    launched = {}
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, **kw: launched.update(kw))
    a = agents.spawn(db, ws, cheap_profile, prompt="fix it", mode="assign", done_when="tests pass")
    assert a.headless == 1 and db.get_agent(a.id).headless == 1
    # a.task stays the raw prompt (see agents.decorate_worker_prompt); the
    # finish line and footer are added to what's actually launched.
    assert a.task == "fix it" and a.done_when == "tests pass"
    prompt = launched["prompt"]
    assert not prompt.startswith("/goal") and "Finish line: tests pass" in prompt


def test_headless_only_applies_to_claude(db, ws, cheap_profile, monkeypatch):
    monkeypatch.setattr(agents, "_launch", lambda *a, **k: None)
    a = agents.spawn(db, ws, cheap_profile, prompt="x", provider_name="shell", mode="assign")
    assert not a.headless


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_headless_worker_lifecycle_in_tmux(db, ws, fake_claude, cheap_profile):
    a = agents.spawn(db, ws, cheap_profile, prompt="build it", mode="assign")
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (fake_claude() and db.get_agent(a.id).status == "idle"):
            time.sleep(0.2)
        first = fake_claude()[0]
        assert first[0] == "-p" and "--strict-mcp-config" in first
        assert first[first.index("--setting-sources") + 1] == "project,local"
        assert first[first.index("--effort") + 1] == "low"
        assert "build it" in first[-1]
        assert agents.is_alive(db.get_agent(a.id))  # the runner waits between turns

        assert agents.send_message(db, a.id, "now the docs") == "delivered"
        deadline = time.time() + 20
        while time.time() < deadline and len(fake_claude()) < 2:
            time.sleep(0.2)
        second = fake_claude()[1]
        assert "--resume" in second and second[-1] == "now the docs"
        assert second[second.index("--resume") + 1] == db.get_agent(a.id).session_ref

        # A turn that fails ends the worker, and waiting on it says why.
        deadline = time.time() + 20
        while time.time() < deadline and db.get_agent(a.id).status != "idle":
            time.sleep(0.2)
        agents.send_message(db, a.id, "FAIL now")
        with pytest.raises(agents.AgentError) as err:
            agents.wait_for_result(db, a.id, timeout=20, poll=0.2)
        assert "exited without reporting" in str(err.value)
        assert "API Error: overloaded" in str(err.value)
        with pytest.raises(agents.AgentError, match="not running"):
            agents.send_message(db, a.id, "hello?")
    finally:
        tmux.kill_session(ws.tmux_session)
