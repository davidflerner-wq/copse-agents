"""Starting the local model server the native profiles need, in the background.

A native profile points at an endpoint; when that endpoint is Ollama on this
machine and it isn't answering, copse starts it so a supervisor can hand work
to ``developer-local`` or get a ``reviewer-local`` review without anyone
remembering to run ``ollama serve`` first. It is started the way ``copse
doctor`` recommends: with ``OLLAMA_CONTEXT_LENGTH`` covering the largest
``context_tokens`` any profile asks of it, since Ollama truncates a
conversation silently past its window. Then each model is loaded once, so
the first turn doesn't wait on a 20 GB read.

Only Ollama on a loopback address is started. A remote host, another
server's port, or a machine without ``ollama`` on PATH is left alone: those
are someone else's to run. ``"local_models": false`` in the repo config turns
all of this off.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from copse.config import RepoConfig, copse_home
from copse.native.client import Endpoint
from copse.profiles import Profile, list_profiles

LOG_NAME = "ollama.log"
KEEP_ALIVE = "30m"          # how long a warmed model stays in memory unused
CONTEXT_HEADROOM = 8192     # what doctor adds over context_tokens for the reply
START_TIMEOUT = 20.0        # seconds to wait for a fresh server to answer
WARM_TIMEOUT = 300.0        # a large model can take minutes to load


@dataclass
class Server:
    """One local server and the profiles that need it."""

    host: str                                  # http://127.0.0.1:11434
    models: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    context_tokens: int = 0                    # the largest any profile asks

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint(self.host + "/v1", self.models[0] if self.models else "")


def log_path() -> Path:
    return copse_home() / LOG_NAME


def is_local(base_url: str) -> bool:
    """Whether ``base_url`` is on this machine (loopback), not just any host."""
    name = (urlparse(base_url).hostname or "").lower()
    return name in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _host(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def local_servers(repo_root: str | None) -> list[Server]:
    """The loopback endpoints the native profiles point at, one per host."""
    from copse.native import runner

    by_host: dict[str, Server] = {}
    for p in list_profiles(repo_root):
        if p.provider != "native":
            continue
        try:
            ep = runner.endpoint_for(p)
        except ValueError:
            continue
        if not is_local(ep.base_url):
            continue
        host = _host(ep.base_url)
        s = by_host.setdefault(host, Server(host))
        if ep.model not in s.models:
            s.models.append(ep.model)
        s.profiles.append(p.name)
        s.context_tokens = max(s.context_tokens, p.context_tokens or 0)
    return list(by_host.values())


def reachable(server: Server, timeout: float = 1.0) -> bool:
    from copse.native import runner

    ok, _ = runner.probe(server.endpoint, timeout=timeout)
    return ok


def ollama_available() -> bool:
    return shutil.which("ollama") is not None


def ollama_env(server: Server, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment ``ollama serve`` gets: the caller's, plus the context
    length doctor would recommend (unless already set) and the host when it
    isn't Ollama's default."""
    out = dict(os.environ if env is None else env)
    if server.context_tokens and "OLLAMA_CONTEXT_LENGTH" not in out:
        out["OLLAMA_CONTEXT_LENGTH"] = str(server.context_tokens + CONTEXT_HEADROOM)
    port = urlparse(server.host).port or 11434
    if port != 11434:
        out["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    return out


def start_ollama(server: Server, log: Path | None = None) -> subprocess.Popen:
    """``ollama serve`` detached from copse, its output appended to the log."""
    log = log or log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")  # noqa: SIM115 -- handed to the child, closed with it
    out.write(f"\n== copse: starting ollama serve for {', '.join(server.profiles)} "
              f"at {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
    out.flush()
    return subprocess.Popen(
        ["ollama", "serve"], env=ollama_env(server),
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_reachable(server: Server, timeout: float = START_TIMEOUT, step: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reachable(server, timeout=1.0):
            return True
        time.sleep(step)
    return reachable(server, timeout=1.0)


def warm(server: Server, model: str, timeout: float = WARM_TIMEOUT) -> bool:
    """Load ``model`` into memory and keep it there for a while, so the first
    real request doesn't pay for the load. Ollama's ``/api/generate`` with no
    prompt does exactly that. False when it couldn't (a model that isn't
    pulled, a server that isn't Ollama)."""
    req = urllib.request.Request(
        server.host + "/api/generate",
        data=json.dumps({"model": model, "keep_alive": KEEP_ALIVE}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def needed(repo_root: str | None, cfg: RepoConfig) -> list[Server]:
    """The local servers that aren't answering and that copse can start."""
    if not cfg.local_models or not ollama_available():
        return []
    return [s for s in local_servers(repo_root) if not reachable(s)]


def ensure(repo_root: str | None, cfg: RepoConfig, *, log: Path | None = None) -> list[str]:
    """Start what's needed and warm every model. Returns one line per thing
    done, for the log; nothing is printed. Meant to run detached (``copse
    _local-models``), since loading a model takes a while."""
    lines: list[str] = []
    if not cfg.local_models:
        return lines
    if not ollama_available():
        return lines
    for server in local_servers(repo_root):
        if not reachable(server):
            start_ollama(server, log)
            if not wait_reachable(server):
                lines.append(f"{server.host}: ollama serve didn't answer within {START_TIMEOUT:.0f}s "
                             f"(see {log or log_path()})")
                continue
            lines.append(f"{server.host}: started ollama serve for {', '.join(server.profiles)}")
        for model in server.models:
            if warm(server, model):
                lines.append(f"{server.host}: {model} loaded (kept for {KEEP_ALIVE})")
            else:
                lines.append(f"{server.host}: couldn't load {model}; is it pulled? `ollama pull {model}`")
    return lines
