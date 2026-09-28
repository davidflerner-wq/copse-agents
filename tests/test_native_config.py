"""Configuring native workers: profile fields and env lines, the built-in
local profiles, and what `copse doctor` says about their endpoints."""

import pytest

from copse import agents, doctor, workspaces
from copse.db import Agent
from copse.native.client import Endpoint
from copse.native.runner import probe
from copse.profiles import list_profiles, load_profile
from test_native_loop import FakeEndpoint


@pytest.fixture
def fake():
    ep = FakeEndpoint()
    yield ep
    ep.close()


def test_builtin_local_profiles_are_native_and_complete():
    dev, rev = load_profile("developer-local"), load_profile("reviewer-local")
    for p in (dev, rev):
        assert p.provider == "native" and p.api == "openai"
        assert p.base_url == "http://localhost:11434/v1" and p.model == "qwen3-coder:30b"
        assert p.context_tokens == 32000
        assert "report_result" in dev.prompt and "submit_review" in rev.prompt
    assert dev.permission_mode == "acceptEdits" and any(t.startswith("Bash(git commit") for t in dev.allowed_tools)
    assert rev.permission_mode == "dontAsk" and not any("git commit" in t for t in rev.allowed_tools)
    names = [p.name for p in list_profiles(None)]
    assert "developer-local" in names and "reviewer-local" in names


def test_profile_env_lines_and_context_sizes(tmp_path):
    d = tmp_path / ".copse" / "agents"
    d.mkdir(parents=True)
    (d / "glm.md").write_text(
        "---\nname: glm\nprovider: claude\nmodel: glm-4.7-flash\n"
        "env.ANTHROPIC_BASE_URL: https://api.z.ai/api/anthropic\n"
        "env.ANTHROPIC_AUTH_TOKEN: 'k#1'   # quoted, so the # stays\n"
        "env.ANTHROPIC_API_KEY:\n"
        "context_tokens: 128_000\n---\nhi\n")
    p = load_profile("glm", str(tmp_path))
    assert p.env == {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                     "ANTHROPIC_AUTH_TOKEN": "k#1", "ANTHROPIC_API_KEY": ""}
    assert p.context_tokens == 128000
    (d / "k.md").write_text("---\nname: k\ncontext_tokens: 64k\n---\n")
    assert load_profile("k", str(tmp_path)).context_tokens == 64000
    (d / "bad.md").write_text("---\nname: bad\ncontext_tokens: lots\n---\n")
    assert load_profile("bad", str(tmp_path)).context_tokens is None


def test_profile_env_reaches_the_agents_process(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True)
    (d / "glm.md").write_text("---\nname: glm\nprovider: claude\nenv.ANTHROPIC_BASE_URL: http://h\n"
                              "env.COPSE_AGENT_ID: nope\n---\nhi\n")
    a = Agent("a1", ws.id, "glm", "claude", None, "assign", "starting", "", None, 0.0)
    env = agents.agent_env(ws, "a1", a)
    assert env["ANTHROPIC_BASE_URL"] == "http://h"
    assert env["COPSE_AGENT_ID"] == "a1"  # copse's own variables win
    plain = agents.agent_env(ws, "a1", Agent("a1", ws.id, "developer", "claude", None, "assign", "starting", "", None, 0.0))
    assert "ANTHROPIC_BASE_URL" not in plain


def test_probe_reports_reachability_and_the_model(fake):
    ok, detail = probe(Endpoint(fake.base_url + "/v1", "tiny"))
    assert ok and "tiny is available" in detail
    assert fake.paths[-1] == "/v1/models"
    ok, detail = probe(Endpoint(fake.base_url, "qwen3-coder"))  # a tag-less name matches its tags
    assert ok and "is available" in detail and fake.paths[-1] == "/v1/models"
    ok, detail = probe(Endpoint(fake.base_url + "/v1", "gpt-9"))
    assert ok and "isn't listed" in detail and "tiny" in detail
    ok, detail = probe(Endpoint("http://127.0.0.1:1/v1", "tiny"), timeout=1)
    assert not ok and "not reachable" in detail


def test_doctor_checks_each_native_endpoint_once(repo, fake, monkeypatch):
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True)
    common = f"provider: native\nbase_url: {fake.base_url}/v1\nmodel: tiny\n"
    (d / "one.md").write_text(f"---\nname: one\n{common}---\nhi\n")
    (d / "two.md").write_text(f"---\nname: two\n{common}---\nhi\n")
    (d / "bare.md").write_text("---\nname: bare\nprovider: native\n---\nhi\n")
    # The built-in local profiles point at Ollama, which isn't running here.
    monkeypatch.setattr("copse.native.runner.probe",
                        lambda ep, timeout=3.0: (False, "not reachable: refused") if "11434" in ep.base_url
                        else probe(ep, timeout))
    checks = {c.name: c for c in doctor.native_checks(str(repo))}
    assert checks["model tiny"].level == doctor.OK and "one, two" in checks["model tiny"].detail
    assert checks["profile bare"].level == doctor.FAIL and "no base_url" in checks["profile bare"].detail
    local = checks["model qwen3-coder:30b"]
    assert local.level == doctor.WARN and "ollama pull qwen3-coder:30b" in local.detail
    assert "developer-local" in local.detail and "reviewer-local" in local.detail
    # And they're part of the full report.
    monkeypatch.setattr(doctor, "_tool", lambda *a, **k: doctor.Check(doctor.OK, a[0], "stub"))
    names = [c.name for c in doctor.checks(str(repo))]
    assert "model tiny" in names and "profile bare" in names
