"""Starting Ollama for the native profiles when copse opens (copse.native.serve)."""

from __future__ import annotations

from pathlib import Path

import pytest

from copse.config import RepoConfig
from copse.native import serve


def _profile(root: Path, name: str, base_url: str, model: str = "qwen3-coder:30b",
             context_tokens: str = "32k") -> None:
    (root / ".copse" / "agents").mkdir(parents=True, exist_ok=True)
    (root / ".copse" / "agents" / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: d\nprovider: native\napi: openai\n"
        f"base_url: {base_url}\nmodel: {model}\ncontext_tokens: {context_tokens}\n---\nbody\n"
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COPSE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def fake_ollama(monkeypatch):
    """`ollama` on PATH, nothing reachable, and a Popen that records instead of
    spawning. Yields the recorder; tests flip `up` to make the server answer."""
    state = {"popen": [], "warmed": [], "up": False}

    def probe(endpoint, timeout=3.0):
        return (True, f"reachable; {endpoint.model} is available") if state["up"] else (False, "not reachable")

    class FakePopen:
        pid = 4242

        def __init__(self, argv, env=None, **kw):
            state["popen"].append((argv, env))
            state["up"] = True  # the server comes up as soon as it's started

    monkeypatch.setattr("copse.native.runner.probe", probe)
    monkeypatch.setattr(serve.shutil, "which", lambda name: "/opt/homebrew/bin/ollama" if name == "ollama" else None)
    monkeypatch.setattr(serve.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(serve, "warm", lambda server, model, timeout=0: state["warmed"].append((server.host, model)) or True)
    monkeypatch.setattr(serve.time, "sleep", lambda s: None)
    return state


def test_local_servers_group_profiles_by_host_and_take_the_largest_context(repo):
    _profile(repo, "small", "http://localhost:11434/v1", context_tokens="16k")
    _profile(repo, "big", "http://127.0.0.1:11434/v1", model="llama3:8b", context_tokens="64k")
    _profile(repo, "remote", "http://gpu-box:8000/v1")
    servers = {s.host: s for s in serve.local_servers(str(repo))}
    # The built-ins (developer-local, reviewer-local) share localhost:11434 with "small".
    assert set(servers) == {"http://localhost:11434", "http://127.0.0.1:11434"}
    assert servers["http://127.0.0.1:11434"].models == ["llama3:8b"]
    assert servers["http://127.0.0.1:11434"].context_tokens == 64_000
    assert "qwen3-coder:30b" in servers["http://localhost:11434"].models
    assert "remote" not in [n for s in servers.values() for n in s.profiles]


def test_ensure_starts_ollama_with_doctors_context_length_and_warms_each_model(repo, fake_ollama):
    _profile(repo, "worker", "http://localhost:11434/v1", context_tokens="32k")
    lines = serve.ensure(str(repo), RepoConfig(), log=repo / "ollama.log")
    assert len(fake_ollama["popen"]) == 1
    argv, env = fake_ollama["popen"][0]
    assert argv == ["ollama", "serve"]
    assert env["OLLAMA_CONTEXT_LENGTH"] == str(32_000 + serve.CONTEXT_HEADROOM)
    assert "OLLAMA_HOST" not in env
    assert ("http://localhost:11434", "qwen3-coder:30b") in fake_ollama["warmed"]
    assert any("started ollama serve" in line and "worker" in line for line in lines)
    assert (repo / "ollama.log").read_text().startswith("\n== copse: starting ollama serve")


def test_ensure_leaves_a_running_server_alone_but_still_warms(repo, fake_ollama):
    fake_ollama["up"] = True
    lines = serve.ensure(str(repo), RepoConfig(), log=repo / "ollama.log")
    assert fake_ollama["popen"] == []
    assert fake_ollama["warmed"]
    assert not any("started" in line for line in lines)


def test_ensure_respects_local_models_false_and_a_missing_binary(repo, fake_ollama, monkeypatch):
    assert serve.ensure(str(repo), RepoConfig(local_models=False), log=repo / "l") == []
    assert fake_ollama["popen"] == []
    monkeypatch.setattr(serve.shutil, "which", lambda name: None)
    assert serve.ensure(str(repo), RepoConfig(), log=repo / "l") == []
    assert fake_ollama["popen"] == []


def test_a_non_default_port_sets_ollama_host_and_an_existing_context_env_wins(repo):
    s = serve.Server("http://127.0.0.1:12000", models=["m"], profiles=["p"], context_tokens=8000)
    env = serve.ollama_env(s, {"PATH": "/bin"})
    assert env["OLLAMA_HOST"] == "127.0.0.1:12000"
    assert env["OLLAMA_CONTEXT_LENGTH"] == str(8000 + serve.CONTEXT_HEADROOM)
    env = serve.ollama_env(s, {"OLLAMA_CONTEXT_LENGTH": "99999"})
    assert env["OLLAMA_CONTEXT_LENGTH"] == "99999"


def test_needed_lists_only_unreachable_local_servers(repo, fake_ollama):
    assert [s.host for s in serve.needed(str(repo), RepoConfig())] == ["http://localhost:11434"]
    fake_ollama["up"] = True
    assert serve.needed(str(repo), RepoConfig()) == []
    assert serve.needed(str(repo), RepoConfig(local_models=False)) == []


def test_repo_config_reads_local_models(repo):
    from copse.config import load_repo_config

    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text('{"local_models": false}')
    assert load_repo_config(str(repo)).local_models is False
    (repo / ".copse" / "config.json").write_text("{}")
    assert load_repo_config(str(repo)).local_models is True


# -- Stopping the server copse started once no session uses it.

SERVE = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"


@pytest.fixture
def fake_serve():
    """A process whose command line reads like `ollama serve`, leading its own
    process group with a child standing in for the model runner."""
    import os
    import signal
    import subprocess
    import sys

    started = []

    def launch():
        p = subprocess.Popen([sys.executable, "-c", SERVE, "ollama", "serve"], start_new_session=True)
        started.append(p)
        return p

    yield launch
    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _group_gone(pgid: int, timeout: float = 5.0) -> bool:
    import subprocess
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True).stdout
        if not any(line.split()[0] == str(pgid) and not line.split()[1].startswith("Z")
                   for line in out.splitlines() if line.strip()):
            return True
        time.sleep(0.1)
    return False


def test_ensure_records_the_server_it_starts(repo, fake_ollama):
    serve.ensure(str(repo), RepoConfig(), log=repo / "ollama.log")
    assert serve.started_servers() == {"http://localhost:11434": 4242}


def test_stop_unused_stops_the_server_and_its_runner_when_no_session_uses_it(db, monkeypatch, fake_serve):
    p = fake_serve()
    serve.record_started("http://localhost:11434", p.pid)
    monkeypatch.setattr(serve, "hosts_in_use", lambda db: set())
    lines = serve.stop_unused(db, grace=2.0)
    assert len(lines) == 1 and f"pid {p.pid}" in lines[0]
    p.wait(timeout=5)
    assert _group_gone(p.pid)
    assert serve.started_servers() == {}
    assert not serve.pids_path().exists()


def test_stop_unused_keeps_a_server_a_running_session_uses(db, monkeypatch, fake_serve):
    p = fake_serve()
    serve.record_started("http://localhost:11434", p.pid)
    monkeypatch.setattr(serve, "hosts_in_use", lambda db: {"http://localhost:11434"})
    assert serve.stop_unused(db) == []
    assert p.poll() is None
    assert serve.started_servers() == {"http://localhost:11434": p.pid}


def test_stop_unused_forgets_a_pid_that_is_no_longer_ollama(db, monkeypatch):
    import os

    serve.record_started("http://localhost:11434", os.getpid())  # this test run, not ollama
    monkeypatch.setattr(serve, "hosts_in_use", lambda db: set())
    assert serve.stop_unused(db) == []
    assert serve.started_servers() == {}


def test_a_server_copse_did_not_start_is_never_stopped(db, monkeypatch):
    monkeypatch.setattr(serve, "hosts_in_use", lambda db: set())
    killed = []
    monkeypatch.setattr(serve, "_stop_group", lambda pid, grace: killed.append(pid))
    assert serve.stop_unused(db) == []
    assert killed == []


def _root_ws(db, repo):
    import time

    from copse.db import Workspace

    ws = Workspace("ws000001", str(repo), "proj", "root", "main", None, str(repo), None,
                   "copse-test-proj", time.time())
    db.add_workspace(ws)
    return ws


def test_hosts_in_use_counts_only_live_or_just_started_session_roots(db, repo, monkeypatch):
    import time

    from copse.db import Agent

    ws = _root_ws(db, repo)
    old = time.time() - 3600

    def root(agent_id, status, created):
        db.add_agent(Agent(agent_id, ws.id, "supervisor", "claude", None, "interactive",
                           status, "", None, created))

    root("paused01", "paused", old)
    root("gone0001", "idle", old)          # its window is gone
    assert serve.hosts_in_use(db) == set()
    root("fresh001", "idle", time.time())  # its window isn't recorded yet
    assert serve.hosts_in_use(db) == {"http://localhost:11434"}


def test_pausing_the_last_session_stops_the_server(db, repo, monkeypatch):
    import time

    from copse import agents
    from copse.db import Agent

    ws = _root_ws(db, repo)
    db.add_agent(Agent("root0001", ws.id, "supervisor", "claude", None, "interactive",
                       "idle", "", None, time.time() - 3600))
    calls = []
    monkeypatch.setattr(serve, "stop_unused", lambda db: calls.append(db) or [])
    agents.pause(db, "root0001", stop_procs=False)
    assert len(calls) == 1
    db.set_status("root0001", "idle")
    agents.pause(db, "root0001", stop_procs=False, stop_local_models=False)
    assert len(calls) == 1
