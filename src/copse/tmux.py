"""Minimal tmux driver: one session per workspace, one window per agent."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid


class TmuxError(RuntimeError):
    pass


def _base() -> list[str]:
    """``tmux``, or ``tmux -L <name>`` when COPSE_TMUX_SOCKET selects a
    private server (used by the test suite so runs can't collide)."""
    sock = os.environ.get("COPSE_TMUX_SOCKET")
    return ["tmux", "-L", sock] if sock else ["tmux"]


def _tmux(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("tmux"):
        raise TmuxError("tmux is not installed (macOS: `brew install tmux`)")
    proc = subprocess.run([*_base(), *args], capture_output=True, text=True, input=input)
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
    """Open a window running ``command``. Returns the agent's PANE id (``%<n>``),
    not the window's: a window can hold more than one pane (e.g. the watch
    dashboard beside a supervisor), and keys sent to a window go to whichever
    pane happens to be active."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
        "-n", name, "-c", cwd, *env_args, "--", *command,
    )
    target = proc.stdout.strip()
    # Keep the agent's pane around after it exits so its output can be read.
    _tmux("set-option", "-p", "-t", target, "remain-on-exit", "on", check=False)
    return target


def split_below(target: str, cwd: str, command: list[str], env: dict[str, str],
                lines: int = 14) -> str:
    """Open a pane under ``target`` running ``command``, keeping focus on
    ``target``. Returns the new pane's id."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "split-window", "-d", "-v", "-l", str(lines), "-P", "-F", "#{pane_id}",
        "-t", target, "-c", cwd, *env_args, "--", *command,
    )
    return proc.stdout.strip()


def window_alive(target: str) -> bool:
    proc = _tmux("display-message", "-p", "-t", target, "#{pane_dead}", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "0"


def kill_window(target: str) -> None:
    _tmux("kill-window", "-t", target, check=False)


def kill_server() -> None:
    _tmux("kill-server", check=False)


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
    return [*_base(), "attach-session", "-t", target]


def select_window(target: str) -> None:
    """Focus the window holding ``target`` and, for a pane id, that pane."""
    _tmux("select-window", "-t", target, check=False)
    if target.startswith("%"):
        _tmux("select-pane", "-t", target, check=False)
