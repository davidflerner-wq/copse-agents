"""Minimal tmux driver: one session per workspace, one window per agent."""

from __future__ import annotations

import os
import shlex
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


def windows(session: str) -> list[str]:
    proc = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


# PawDelta palette (pawdelta.com): near-black ground, indigo accent, slate text.
THEME = {
    "bg": "#0a0b0f", "bg2": "#111318", "line": "#1f2230",
    "accent": "#6366f1", "accent_light": "#818cf8",
    "text": "#f1f5f9", "muted": "#64748b", "muted2": "#94a3b8",
}


def apply_theme(session: str) -> None:
    """Style one copse session (never the person's global tmux config)."""
    t = THEME
    opts = {
        "status-style": f"bg={t['bg2']},fg={t['muted2']}",
        "status-left": f"#[bg={t['accent']},fg={t['text']},bold] copse #[bg={t['bg2']},fg={t['accent']}] ",
        "status-left-length": "20",
        "status-right": f"#[fg={t['muted']}]#{{session_name}}  %H:%M ",
        "status-right-length": "60",
        "window-status-format": f"#[fg={t['muted']}] #W ",
        "window-status-current-format": f"#[fg={t['accent_light']},bold] #W ",
        "pane-border-style": f"fg={t['line']}",
        "pane-active-border-style": f"fg={t['accent']}",
        "pane-border-lines": "single",
        "window-style": f"bg={t['bg']}",
        "window-active-style": f"bg={t['bg']}",
        "message-style": f"bg={t['accent']},fg={t['text']}",
        "mode-style": f"bg={t['accent']},fg={t['text']}",
        # Wheel scrolling and click-to-focus between the sidebar and the chat.
        "mouse": "on",
    }
    # Lets Claude Code notice when its pane gains or loses focus (it asks for
    # this). Server-wide in tmux, and harmless for other sessions.
    _tmux("set-option", "-s", "focus-events", "on", check=False)
    # set-option doesn't accept the "=name" exact-match form other commands do.
    for key, value in opts.items():
        _tmux("set-option", "-t", session, key, value, check=False)
    for key in ("window-style", "window-active-style", "pane-border-style",
                "pane-active-border-style", "pane-border-lines", "mode-style",
                "window-status-format", "window-status-current-format"):
        # window options: set on every window of the session
        for win in _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id}", check=False).stdout.split():
            _tmux("set-option", "-w", "-t", win, key, opts[key], check=False)
    set_follow_hooks(session)


# Fired whenever a copse session's active window changes (picking a different
# window with the mouse, `next-window`, ...) or a client switches into it
# (switch-client from elsewhere): both are how the sidebar's home window can
# change, so both relocate it (see agents.sidebar_follow). Session-scoped
# only, like the rest of this function: never -g, so a plain tmux session the
# person opened themselves is never touched.
FOLLOW_HOOKS = ("session-window-changed", "client-session-changed")


def set_follow_hooks(session: str) -> None:
    from copse.providers import copse_invocation

    cmd = " ".join(shlex.quote(a) for a in [*copse_invocation(), "_sidebar-follow", session])
    for hook in FOLLOW_HOOKS:
        _tmux("set-hook", "-t", session, hook, f"run-shell -b {shlex.quote(cmd)}", check=False)


def split_left(target: str, cwd: str, command: list[str], env: dict[str, str],
               columns: int = 30) -> str:
    """Open a narrow pane to the LEFT of ``target`` running ``command``,
    keeping focus on ``target``. Returns the new pane's id."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "split-window", "-d", "-h", "-b", "-l", str(columns), "-P", "-F", "#{pane_id}",
        "-t", target, "-c", cwd, *env_args, "--", *command,
    )
    pane = proc.stdout.strip()
    # tmux grows every pane proportionally when a client attaches at a bigger
    # size; keep the sidebar at its width whenever the window is resized.
    _tmux("set-hook", "-w", "-t", target, "window-resized",
          f"resize-pane -t {pane} -x {columns}", check=False)
    return pane


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


def kill_pane(target: str) -> None:
    _tmux("kill-pane", "-t", target, check=False)


def pane_window(pane: str) -> str | None:
    """The id of the window ``pane`` is currently in, or None if it's gone."""
    proc = _tmux("display-message", "-p", "-t", pane, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def active_window(session: str) -> str | None:
    """The id of ``session``'s currently active window, or None if the
    session doesn't exist."""
    proc = _tmux("display-message", "-p", "-t", session, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def agent_pane_in_window(window: str, sidebar: str | None) -> str | None:
    """The pane to anchor the sidebar beside in ``window``: its active pane,
    or (if that's the sidebar itself) any other pane there. None if the
    sidebar is the only pane (never join it onto itself)."""
    proc = _tmux("display-message", "-p", "-t", window, "#{pane_id}", check=False)
    active = proc.stdout.strip() if proc.returncode == 0 else None
    if active and active != sidebar:
        return active
    panes = _tmux("list-panes", "-t", window, "-F", "#{pane_id}", check=False).stdout.split()
    others = [p for p in panes if p != sidebar]
    return others[0] if others else None


def move_pane(pane: str, target: str, columns: int = 30) -> None:
    """Relocate ``pane`` (e.g. the sidebar) to sit at the left of ``target``'s
    window, keeping focus on whatever's already active there. Re-points the
    window-resize pin (see split_left) at the new window and clears it from
    the old one, so a resize never tries to resize a pane that's moved on."""
    old_window = pane_window(pane)
    _tmux("join-pane", "-h", "-b", "-d", "-l", str(columns), "-s", pane, "-t", target, check=False)
    if old_window:
        _tmux("set-hook", "-w", "-t", old_window, "-u", "window-resized", check=False)
    _tmux("set-hook", "-w", "-t", target, "window-resized",
          f"resize-pane -t {pane} -x {columns}", check=False)


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
