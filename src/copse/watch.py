"""`copse watch`: a live view of every workspace and agent.

``render`` turns a snapshot into styled lines and knows nothing about the
terminal; ``run`` is a thin curses loop around it. ``--once`` prints a
single render without curses.
"""

from __future__ import annotations

import curses
import os
import subprocess
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


def summary(snap: list[dict]) -> str:
    agents_ = [a for ws in snap for a in ws["agents"]]
    waiting = sum(a["status"] == "waiting" for a in agents_)
    busy = sum(a["status"] == "processing" for a in agents_)
    parts = [f"{len(snap)} workspace{'s' * (len(snap) != 1)}",
             f"{len(agents_)} agent{'s' * (len(agents_) != 1)}"]
    if busy:
        parts.append(f"{busy} working")
    if waiting:
        parts.append(f"{waiting} waiting for you")
    return " · ".join(parts)


def render(snap: list[dict], now: float) -> list[Line]:
    lines: list[Line] = [Line(summary(snap), "bold"), Line("")]
    if not snap:
        lines.append(Line("No workspaces. Start one with `copse new <branch>` or `copse start`.", "dim"))
        return lines
    for ws in snap:
        git_info = ""
        if ws["ahead"] is not None:
            dirty = f"  {ws['dirty']} uncommitted" if ws["dirty"] else ""
            git_info = f"  ↑{ws['ahead']} ↓{ws['behind']} vs {ws['base_branch']}{dirty}"
        elif not os.path.isdir(ws["path"]):
            git_info = "  (worktree missing)"
        lines.append(Line(f"{ws['id']}  [{ws['branch']}]{git_info}", "bold", workspace=ws))
        if not ws["agents"]:
            lines.append(Line("    no agents", "dim", workspace=ws))
        for a in ws["agents"]:
            since = a.get("status_since")
            age = ago(now - since) if since else ""
            status = a["status"] + (f" {age}" if age else "")
            extras = []
            if a.get("pending"):
                extras.append(f"{a['pending']} queued msg{'s' * (a['pending'] != 1)}")
            if a.get("reported"):
                extras.append("reported")
            if a["status"] == "waiting":
                extras.append("needs approval")
            text = (f"    {a['id']}  {a['profile']:<11} {a['provider']:<6} "
                    f"{status:<16} {a['mode']:<16} {', '.join(extras)}")
            lines.append(Line(text.rstrip(), STATUS_STYLE.get(a["status"], "normal"),
                              agent=a, workspace=ws))
        lines.append(Line(""))
    return lines


# -- --once ------------------------------------------------------------------

ANSI = {"bold": "1", "dim": "2", "busy": "36", "ok": "32", "alert": "1;33", "bad": "31"}


def print_once(db: DB, repo_root: str | None, color: bool) -> str:
    out = []
    for line in render(view.snapshot(db, repo_root), time.time()):
        code = ANSI.get(line.style) if color else None
        out.append(f"\033[{code}m{line.text}\033[0m" if code else line.text)
    return "\n".join(out)


# -- interactive ---------------------------------------------------------------

HELP = "↑/↓ select   enter attach   p peek   r refresh   q quit"


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
        subprocess.run(["tmux", "switch-client", "-t", agent["window"]])
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
        if stale:
            lines = render(view.snapshot(db, repo_root), time.time())
            stale = False
        rows = [i for i, ln in enumerate(lines) if ln.agent]
        selected = max(0, min(selected, len(rows) - 1))

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        stdscr.addnstr(0, 0, f"copse watch · {time.strftime('%H:%M:%S')}", w - 1, styles["dim"])
        for y, (i, ln) in enumerate(enumerate(lines[: h - 3]), start=1):
            attr = styles.get(ln.style, curses.A_NORMAL)
            if rows and i == rows[selected]:
                attr |= curses.A_REVERSE
            stdscr.addnstr(y, 0, ln.text, w - 1, attr)
        stdscr.addnstr(h - 1, 0, HELP, w - 1, styles["dim"])
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
