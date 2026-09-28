"""copse doctor warns when an Ollama server's context is smaller than a
native profile's context_tokens."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from copse import doctor
from copse.native import runner


class FakeOllama:
    """Answers /v1/models always; /api/version and /api/show only when
    ``ollama`` is set. ``parameters`` is the string /api/show reports."""

    def __init__(self, ollama=True, parameters=""):
        self.ollama = ollama
        self.parameters = parameters
        self.show_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": "tiny"}]})
                elif self.path == "/api/version" and outer.ollama:
                    self._send(200, {"version": "0.9.0"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.show_requests.append(json.loads(self.rfile.read(n)))
                if self.path == "/api/show" and outer.ollama:
                    self._send(200, {"parameters": outer.parameters})
                else:
                    self._send(404, {"error": "not found"})

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
def serve():
    servers = []

    def make(**kw):
        s = FakeOllama(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def add_profile(repo, fake, context="8k"):
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "local.md").write_text(
        f"---\nname: local\ndescription: a local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: {context}\n"
        "permission_mode: acceptEdits\n---\nYou are a local worker.\n"
    )


def context_checks(repo):
    return [c for c in doctor.native_checks(str(repo)) if c.name == "context local"]


def test_small_num_ctx_warns_with_the_fix(repo, serve):
    fake = serve(parameters="num_ctx                        4096\nstop  <|im_end|>")
    add_profile(repo, fake)
    [c] = context_checks(repo)
    assert c.level == doctor.WARN
    assert "4096" in c.detail and "8000" in c.detail
    assert "OLLAMA_CONTEXT_LENGTH=16192 ollama serve" in c.detail
    assert fake.show_requests == [{"model": "tiny"}]


def test_large_num_ctx_is_quiet(repo, serve):
    fake = serve(parameters="num_ctx 32768")
    add_profile(repo, fake)
    assert context_checks(repo) == []


def test_unreported_context_warns(repo, serve):
    fake = serve(parameters="stop <|im_end|>")
    add_profile(repo, fake)
    [c] = context_checks(repo)
    assert c.level == doctor.WARN
    assert "doesn't report its context length" in c.detail
    assert "OLLAMA_CONTEXT_LENGTH=16192" in c.detail


def test_non_ollama_endpoint_has_no_context_check(repo, serve):
    fake = serve(ollama=False)
    add_profile(repo, fake)
    assert context_checks(repo) == []
    assert runner.server_context(runner.Endpoint(fake.base_url + "/v1", "tiny")) is None
    assert fake.show_requests == []


def test_server_context_reads_num_ctx(serve):
    fake = serve(parameters="num_ctx 12345")
    assert runner.server_context(runner.Endpoint(fake.base_url + "/v1", "tiny")) == 12345


def test_server_context_none_on_error():
    assert runner.server_context(runner.Endpoint("http://127.0.0.1:1/v1", "tiny"), timeout=0.5) is None


def test_two_profiles_same_endpoint_no_context(repo, serve):
    """Test that when two native profiles share an endpoint that reports no context length,
    only one WARN check is produced, with both profile names in the check name."""
    fake = serve(parameters="stop")  # No num_ctx parameter, so context is None
    
    # Add two profiles using the same endpoint 
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    
    # First custom profile
    (d / "custom1.md").write_text(
        f"---\nname: custom1\ndescription: a local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: 8000\n"
        "permission_mode: acceptEdits\n---\nYou are a local worker.\n"
    )
    
    # Second custom profile  
    (d / "custom2.md").write_text(
        f"---\nname: custom2\ndescription: another local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: 16000\n"
        "permission_mode: acceptEdits\n---\nYou are a second local worker.\n"
    )
    
    checks = doctor.native_checks(str(repo))
    context_checks = [c for c in checks if c.name.startswith("context ") and "custom" in c.name]
    
    # Should have exactly one context check  
    assert len(context_checks) == 1
    
    # Check that both profile names are in the check name
    check = context_checks[0]
    assert "custom1, custom2" in check.name
    assert check.level == doctor.WARN
    assert "doesn't report its context length" in check.detail
    # Should use the larger context_tokens (16000) for the fix hint
    assert "OLLAMA_CONTEXT_LENGTH=24192" in check.detail  # 16000 + 8192


def test_two_profiles_same_endpoint_small_context(repo, serve):
    """Test that when two native profiles share an endpoint with small context,
    only one WARN check is produced, using the maximum context_tokens for the fix hint."""
    fake = serve(parameters="num_ctx 4096\nstop")  # Small context but has num_ctx parameter
    
    # Add two profiles using the same endpoint
    d = repo / ".copse" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    
    # First custom profile
    (d / "custom3.md").write_text(
        f"---\nname: custom3\ndescription: a local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: 8000\n"
        "permission_mode: acceptEdits\n---\nYou are a local worker.\n"
    )
    
    # Second custom profile  
    (d / "custom4.md").write_text(
        f"---\nname: custom4\ndescription: another local model\nprovider: native\napi: openai\n"
        f"base_url: {fake.base_url}/v1\nmodel: tiny\ncontext_tokens: 16000\n"
        "permission_mode: acceptEdits\n---\nYou are a second local worker.\n"
    )
    
    checks = doctor.native_checks(str(repo))
    context_checks = [c for c in checks if c.name.startswith("context ") and "custom" in c.name]
    
    # Should have exactly one context check
    assert len(context_checks) == 1
    
    # Check that both profile names are in the check name
    check = context_checks[0]
    assert "custom3, custom4" in check.name
    assert check.level == doctor.WARN
    assert "less than the profile's context_tokens" in check.detail
    # Should use the larger context_tokens (16000) for the fix hint
    assert "OLLAMA_CONTEXT_LENGTH=24192" in check.detail  # 16000 + 8192
