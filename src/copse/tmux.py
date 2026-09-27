"""Minimal tmux driver: one session per workspace, one window per agent."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid


class TmuxError(RuntimeError):
    pass


def _tmux(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("tmux"):
        raise TmuxError("tmux is not installed (macOS: `brew install tmux`)")
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True, input=input)
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def has_session(session: str) -> bool:
    return _tmux("has-session", "-t", f"={session}", check=False).returncode == 0


def ensure_session(session: str, cwd: str, env: dict[str, str]) -> None:
    """Create a detached session whose first window is a plain shell."""
    if has_session(session):
        return
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    _tmux("new-session", "-d", "-s", session, "-n", "shell", "-c", cwd, *env_args)


def new_window(session: str, name: str, cwd: str, command: list[str], env: dict[str, str]) -> str:
    """Open a window running ``command``. Returns a stable target (``@<id>``)."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"={session}:",
        "-n", name, "-c", cwd, *env_args, "--", *command,
    )
    target = proc.stdout.strip()
    # Keep the pane around after the agent exits so its output can be read.
    _tmux("set-option", "-w", "-t", target, "remain-on-exit", "on", check=False)
    return target


def window_alive(target: str) -> bool:
    proc = _tmux("display-message", "-p", "-t", target, "#{pane_dead}", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "0"


def kill_window(target: str) -> None:
    _tmux("kill-window", "-t", target, check=False)


def kill_session(session: str) -> None:
    _tmux("kill-session", "-t", f"={session}", check=False)


def capture(target: str, lines: int = 200) -> str:
    return _tmux("capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}").stdout


def paste(target: str, text: str, submit: bool = True) -> None:
    """Paste ``text`` as one bracketed paste (so newlines don't submit early),
    then press Enter."""
    buf = f"copse-{uuid.uuid4().hex[:8]}"
    _tmux("load-buffer", "-b", buf, "-", input=text)
    _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target)
    if submit:
        # TUIs debounce paste events; Enter too soon gets folded into the paste.
        time.sleep(0.3)
        _tmux("send-keys", "-t", target, "Enter")


def send_keys(target: str, *keys: str) -> None:
    _tmux("send-keys", "-t", target, *keys)


def attach_command(session: str, window: str | None = None) -> list[str]:
    target = f"={session}" if window is None else window
    return ["tmux", "attach-session", "-t", target]


def select_window(target: str) -> None:
    _tmux("select-window", "-t", target, check=False)
