"""`copse watch`: a live view of every workspace and agent.

``render`` turns a snapshot into styled lines and knows nothing about the
terminal; ``run`` is a thin curses loop around it. ``--once`` prints a
single render without curses.
"""

from __future__ import annotations

import curses
import os
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass

from copse import tmux, view
from copse.db import DB

REFRESH_SECONDS = 2.0

# Styles are names, mapped to curses attributes (or ANSI for --once) later.
STATUS_STYLE = {
    "processing": "busy",
    "starting": "busy",
    "idle": "ok",
    "waiting": "alert",
    "exited": "bad",
    "paused": "dim",
    "done": "ok",
}

# How each status reads on screen: (icon, label).
STATUS_LABEL = {
    "processing": ("●", "working"),
    "starting": ("◌", "starting up"),
    "idle": ("○", "idle"),
    "waiting": ("!", "needs you"),
    "exited": ("✕", "stopped"),
}


@dataclass
class Line:
    text: str
    style: str = "normal"          # normal | dim | bold | busy | ok | alert | bad
    agent: dict | None = None      # set on agent rows; these are selectable
    workspace: dict | None = None


def ago(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"
    return f"{int(seconds // 86400)}d"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def summary(snap: list[dict]) -> str:
    agents_ = [a for ws in snap for a in ws["agents"]]
    waiting = sum(a["status"] == "waiting" for a in agents_)
    busy = sum(a["status"] in ("processing", "starting") for a in agents_)
    parts = []
    if waiting:
        parts.append(f"{waiting} need{'s' * (waiting == 1)} you")
    if busy:
        parts.append(f"{busy} working")
    if not parts:
        parts.append("all quiet")
    return " · ".join(parts)


def git_summary(ws: dict) -> str:
    if ws["ahead"] is None:
        return "folder missing" if not os.path.isdir(ws["path"]) else ""
    parts = []
    if ws["ahead"]:
        parts.append(f"{ws['ahead']} ahead")
    if ws["behind"]:
        parts.append(f"{ws['behind']} behind")
    parts = [" · ".join(parts) + f" {ws['base_branch']}"] if parts else [f"up to date with {ws['base_branch']}"]
    if ws["dirty"]:
        parts.append(f"{plural(ws['dirty'], 'file')} changed")
    return " · ".join(parts)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    return textwrap.wrap(text, max(width, len(indent) + 8), initial_indent=indent,
                         subsequent_indent=indent) or [indent]


def render(snap: list[dict], now: float, width: int = 80) -> list[Line]:
    agents_ = [a for ws in snap for a in ws["agents"]]
    lines = [Line(summary(snap), "alert" if any(a["status"] == "waiting" for a in agents_) else "bold")]
    if snap:
        lines.append(Line(f"{plural(len(agents_), 'agent')} in {plural(len(snap), 'workspace')}", "dim"))
    lines.append(Line(""))
    if not snap:
        for t in _wrap("Nothing running yet. Start an agent with `copse new <branch>`.", width, ""):
            lines.append(Line(t, "dim"))
        return lines
    for ws in snap:
        title = ws["branch"] + ("  (your checkout)" if ws.get("name") == "root" else "")
        lines.append(Line(title, "bold", workspace=ws))
        if (info := git_summary(ws)):
            lines += [Line(t, "dim", workspace=ws) for t in _wrap(info, width, "  ")]
        if not ws["agents"]:
            lines.append(Line("  no agents", "dim", workspace=ws))
        for a in ws["agents"]:
            icon, label = STATUS_LABEL.get(a["status"], ("·", a["status"]))
            if a["status"] == "idle" and a.get("reported"):
                icon, label = "✓", "done"
            name = a["profile"].replace("-", " ").capitalize()
            if a["provider"] != "claude":
                name += f" ({a['provider']})"
            lines.append(Line(f"  {icon} {name}", STATUS_STYLE.get(a["status"], "normal"),
                              agent=a, workspace=ws))
            since = a.get("status_since")
            detail = [label + (f" for {ago(now - since)}" if since else "")]
            if a.get("pending"):
                detail.append(f"{plural(a['pending'], 'message')} queued")
            detail.append(a["id"][:6])
            lines += [Line(t, "dim", workspace=ws) for t in _wrap(" · ".join(detail), width, "    ")]
        lines.append(Line(""))
    return lines


# -- --once ------------------------------------------------------------------

ANSI = {"bold": "1", "dim": "2", "busy": "36", "ok": "32", "alert": "1;33", "bad": "31"}


def print_once(db: DB, repo_root: str | None, color: bool) -> str:
    out = []
    width = shutil.get_terminal_size().columns - 1
    for line in render(view.snapshot(db, repo_root), time.time(), width):
        code = ANSI.get(line.style) if color else None
        out.append(f"\033[{code}m{line.text}\033[0m" if code else line.text)
    return "\n".join(out)


# -- interactive ---------------------------------------------------------------

HELP = ["↑↓ ⏎ open  p peek  q quit"]
HELP_IN_TMUX = [*HELP, "prefix L: back from agent"]


def _styles() -> dict[str, int]:
    attrs = {"normal": curses.A_NORMAL, "bold": curses.A_BOLD, "dim": curses.A_DIM}
    if curses.has_colors():
        curses.use_default_colors()
        for i, (name, color) in enumerate(
            [("busy", curses.COLOR_CYAN), ("ok", curses.COLOR_GREEN),
             ("alert", curses.COLOR_YELLOW), ("bad", curses.COLOR_RED)], start=1):
            curses.init_pair(i, color, -1)
            attrs[name] = curses.color_pair(i)
        attrs["alert"] |= curses.A_BOLD
    else:
        attrs.update(busy=curses.A_NORMAL, ok=curses.A_NORMAL,
                     alert=curses.A_BOLD, bad=curses.A_DIM)
    return attrs


def _attach(agent: dict, ws: dict, db: DB) -> None:
    """Leave curses, attach to the agent's tmux window, come back on detach."""
    record = db.get_workspace(ws["id"])
    if not record or not agent.get("window"):
        return
    tmux.select_window(agent["window"])
    curses.endwin()
    if os.environ.get("TMUX"):
        subprocess.run([*tmux._base(), "switch-client", "-t", agent["window"]])
    else:
        subprocess.run(tmux.attach_command(record.tmux_session))


def _peek(stdscr, agent: dict, styles: dict[str, int]) -> None:
    try:
        screen = tmux.capture(agent["window"], lines=200).rstrip().splitlines()
    except tmux.TmuxError as e:
        screen = [f"(can't read the agent's terminal: {e})"]
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    title = f" {agent['id']} ({agent['profile']}) — last lines of its terminal · any key to go back "
    stdscr.addnstr(0, 0, title, w - 1, styles["bold"] | curses.A_REVERSE)
    body = screen[-(h - 2):]
    for i, text in enumerate(body, start=1):
        stdscr.addnstr(i, 0, text, w - 1)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
    stdscr.timeout(int(REFRESH_SECONDS * 1000))


def _loop(stdscr, repo_root: str | None) -> None:
    curses.curs_set(0)
    styles = _styles()
    stdscr.timeout(int(REFRESH_SECONDS * 1000))
    db = DB()
    selected = 0
    lines: list[Line] = []
    stale = True
    while True:
        h, w = stdscr.getmaxyx()
        if stale:
            lines = render(view.snapshot(db, repo_root), time.time(), w - 1)
            stale = False
        rows = [i for i, ln in enumerate(lines) if ln.agent]
        selected = max(0, min(selected, len(rows) - 1))

        help_ = [t for text in (HELP_IN_TMUX if os.environ.get("TMUX") else HELP)
                 for t in _wrap(text, w - 1, "")]
        stdscr.erase()
        clock = time.strftime("%H:%M")
        stdscr.addnstr(0, 0, "COPSE", w - 1, styles["bold"])
        if w > len(clock) + 7:
            stdscr.addnstr(0, w - 1 - len(clock), clock, len(clock), styles["dim"])
        for y, (i, ln) in enumerate(enumerate(lines[: h - 3 - len(help_)]), start=2):
            attr = styles.get(ln.style, curses.A_NORMAL)
            if rows and i == rows[selected]:
                attr |= curses.A_REVERSE
            stdscr.addnstr(y, 0, ln.text, w - 1, attr)
        for y, text in enumerate(help_, start=h - len(help_)):
            stdscr.addnstr(y, 0, text, w - 1, styles["dim"])
        stdscr.refresh()

        key = stdscr.getch()
        if key == -1 or key in (ord("r"), curses.KEY_RESIZE):
            stale = True
        elif key in (ord("q"), 27):
            return
        elif key in (curses.KEY_UP, ord("k")):
            selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            selected += 1
        elif rows and key in (curses.KEY_ENTER, 10, 13, ord("a")):
            ln = lines[rows[selected]]
            _attach(ln.agent, ln.workspace, db)
            stdscr.clear()
            stale = True
        elif rows and key == ord("p"):
            _peek(stdscr, lines[rows[selected]].agent, styles)
            stale = True


def run(repo_root: str | None) -> None:
    curses.wrapper(_loop, repo_root)
