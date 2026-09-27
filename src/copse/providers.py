"""How to launch each supported CLI agent and learn when it's idle.

Inferring agent state by regex-matching the terminal screen breaks whenever
a CLI redesigns its TUI. Where the CLI offers lifecycle hooks
(Claude Code), copse uses those instead: the agent itself reports
``processing`` / ``idle`` / ``waiting`` by running ``copse _hook <event>``.
CLIs without hooks report ``unknown``, and messages to them are delivered
immediately rather than queued.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass

from copse import tmux
from copse.profiles import Profile


def copse_invocation() -> list[str]:
    """argv that re-enters this same copse install, independent of PATH."""
    return [sys.executable, "-m", "copse"]


def mcp_server_spec(agent_id: str) -> dict:
    env = {"COPSE_AGENT_ID": agent_id}
    for key in ("COPSE_HOME", "COPSE_TMUX_SOCKET"):
        if key in os.environ:
            env[key] = os.environ[key]
    cmd = copse_invocation()
    return {"command": cmd[0], "args": [*cmd[1:], "mcp"], "env": env}


@dataclass
class LaunchContext:
    agent_id: str
    profile: Profile
    initial_prompt: str | None
    resume: str | None = None   # the CLI's session id to continue, if it supports that


class Provider:
    name = "base"
    uses_hooks = False

    def command(self, ctx: LaunchContext) -> list[str]:
        raise NotImplementedError

    def after_launch(self, target: str) -> None:
        """Handle any startup dialogs. Default: nothing."""

    def screen_state(self, screen: str) -> str | None:
        """Best-effort read of the terminal: 'idle', 'waiting', 'busy', or
        None when unsure. Only used to correct a status hooks left stale."""
        return None


class ClaudeCode(Provider):
    name = "claude"
    uses_hooks = True

    TRUST_DIALOG = re.compile(r"(one you trust|trust (this|the files in this) folder)", re.I)
    # Claude Code's input box, across versions: the "❯" prompt line, the old
    # "? for shortcuts" hint, or a turn already running.
    READY = re.compile(r"^\s*❯|\? for shortcuts|esc to interrupt|⏵⏵", re.M)

    @staticmethod
    def can_resume(session_id: str) -> bool:
        """Claude Code saves a conversation only once something was said in it,
        as <config>/projects/<project>/<session id>.jsonl. Resuming one that
        was never saved exits at once with "No conversation found"."""
        import glob

        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        return bool(glob.glob(os.path.join(glob.escape(config), "projects", "*", f"{glob.escape(session_id)}.jsonl")))
    YES_SELECTED = re.compile(r"[❯>]\s*(\d+\.\s*)?Yes, I trust", re.I)

    def _hook(self, event: str) -> list[dict]:
        cmd = " ".join(f"'{a}'" for a in [*copse_invocation(), "_hook", event])
        return [{"hooks": [{"type": "command", "command": cmd}]}]

    def command(self, ctx: LaunchContext) -> list[str]:
        settings = {
            "hooks": {
                "SessionStart": self._hook("session-start"),
                "UserPromptSubmit": self._hook("prompt-submit"),
                "Stop": self._hook("stop"),
                "Notification": self._hook("notification"),
                # After a permission prompt is answered, the tool runs; flip
                # 'waiting' back to 'processing'.
                "PostToolUse": self._hook("tool-done"),
            }
        }
        mcp = {"mcpServers": {"copse": mcp_server_spec(ctx.agent_id)}}
        argv = [
            "claude",
            "--settings", json.dumps(settings),
            "--mcp-config", json.dumps(mcp),
            "--allowedTools", ",".join(["mcp__copse", *(ctx.profile.allowed_tools or [])]),
        ]
        if ctx.profile.prompt:
            argv += ["--append-system-prompt", ctx.profile.prompt]
        if ctx.profile.model:
            argv += ["--model", ctx.profile.model]
        if ctx.profile.permission_mode:
            argv += ["--permission-mode", ctx.profile.permission_mode]
        if ctx.resume:
            argv += ["--resume", ctx.resume]
        elif ctx.initial_prompt:
            argv.append(ctx.initial_prompt)
        return argv

    def screen_state(self, screen: str) -> str | None:
        tail = "\n".join(screen.rstrip().splitlines()[-25:])
        if "Do you want to proceed?" in tail or "Enter to confirm" in tail:
            return "waiting"
        if "esc to interrupt" in tail:
            return "busy"
        if "? for shortcuts" in tail or "⏵⏵" in tail or "shift+tab to cycle" in tail:
            return "idle"
        return None

    def after_launch(self, target: str) -> None:
        # A fresh worktree is a folder Claude Code hasn't seen, so it asks
        # whether to trust it. copse created the worktree from the user's own
        # repo, so choose "Yes". The dialog's cursor starts on "No, exit", so
        # move it explicitly and never press Enter unless "Yes" is selected.
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                screen = tmux.capture(target, lines=60)
            except tmux.TmuxError:
                return
            if not self.TRUST_DIALOG.search(screen):
                if self.READY.search(screen):
                    return
                continue
            if self.YES_SELECTED.search(screen):
                tmux.send_keys(target, "Enter")
            else:
                tmux.send_keys(target, "Down")


# The ChatGPT desktop app bundles the Codex CLI without putting it on PATH.
CODEX_BUNDLED = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


def codex_binary() -> str:
    """COPSE_CODEX_BIN, else `codex` on PATH, else the ChatGPT app's copy."""
    import shutil

    explicit = os.environ.get("COPSE_CODEX_BIN")
    if explicit:
        return explicit
    return shutil.which("codex") or (CODEX_BUNDLED if os.path.exists(CODEX_BUNDLED) else "codex")


class Codex(Provider):
    name = "codex"

    def command(self, ctx: LaunchContext) -> list[str]:
        spec = mcp_server_spec(ctx.agent_id)
        argv = [
            codex_binary(),
            "-c", f"mcp_servers.copse.command={json.dumps(spec['command'])}",
            "-c", f"mcp_servers.copse.args={json.dumps(spec['args'])}",
            "-c", "mcp_servers.copse.env=" + "{" + ", ".join(
                f"{k} = {json.dumps(v)}" for k, v in spec["env"].items()
            ) + "}",
            # Pre-approve copse's own tools (report_result, send_message, ...),
            # like --allowedTools mcp__copse for Claude Code. Nothing else.
            "-c", 'mcp_servers.copse.default_tools_approval_mode="approve"',
        ]
        if ctx.profile.model:
            argv += ["--model", ctx.profile.model]
        # Codex has no system-prompt flag; lead the first message with the profile.
        first = "\n\n".join(p for p in (ctx.profile.prompt, ctx.initial_prompt) if p)
        if first:
            argv.append(first)
        return argv

    TRUST_DIALOG = re.compile(r"Trust this folder\?", re.I)
    TRUST_SELECTED = re.compile(r"›\s*1\.\s*Trust and continue")

    def after_launch(self, target: str) -> None:
        # Same situation as Claude Code: a new worktree of the user's own repo.
        # Codex saves this trust for the repository root in ~/.codex/config.toml.
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                screen = tmux.capture(target, lines=60)
            except tmux.TmuxError:
                return
            if self.TRUST_DIALOG.search(screen):
                if self.TRUST_SELECTED.search(screen):
                    tmux.send_keys(target, "Enter")
                    return
                tmux.send_keys(target, "Up")
            elif "›" in screen and ("context left" in screen or "Esc to interrupt" in screen):
                return


class Shell(Provider):
    """A plain shell. Useful for dev servers and for testing copse itself."""

    name = "shell"

    def command(self, ctx: LaunchContext) -> list[str]:
        return [os.environ.get("SHELL", "/bin/sh")]


PROVIDERS: dict[str, Provider] = {p.name: p for p in (ClaudeCode(), Codex(), Shell())}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}") from None
