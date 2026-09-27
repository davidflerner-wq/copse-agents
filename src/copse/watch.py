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
    "waiting": ("◆", "needs you"),
    "exited": ("✕", "stopped"),
    "paused": ("‖", "paused"),
    "done": ("✓", "done"),
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


MILESTONE_MARK = {"passed": ("✓", "ok"), "failed": ("✗", "bad"), "pending": ("○", "dim")}


def render_autopilot(pilot: dict, width: int) -> list[Line]:
    """The goal and its milestones, above the agents."""
    if not pilot["enabled"]:
        return [Line("Autopilot off", "dim"), Line("")]
    if not pilot["goal"]:
        lines = [Line("Autopilot on", "accent")]
        lines += [Line(t, "dim") for t in _wrap("Tell the supervisor what we're building.", width, "  ")]
        return lines + [Line("")]
    lines = [Line(t, "accent") for t in _wrap(f"Autopilot · {pilot['goal']}", width, "")]
    for m in pilot["milestones"]:
        mark, style = MILESTONE_MARK.get(m["status"], ("·", "dim"))
        wrapped = _wrap(m["title"], width - 4, "")
        lines.append(Line(f"  {mark} {wrapped[0]}", style))
        lines += [Line(f"    {t}", style) for t in wrapped[1:]]
    done = sum(m["status"] == "passed" for m in pilot["milestones"])
    total = len(pilot["milestones"])
    state = {"done": ("goal reached", "ok"), "blocked": ("needs you", "alert"),
             "stalled": ("stalled: needs you", "alert")}.get(pilot["state"])
    parts = [f"{done} of {total} verified"]
    if state:
        parts.append(state[0])
    elif pilot.get("workers"):
        parts.append(f"{plural(pilot['workers'], 'worker')} on it")
    lines += [Line(t, state[1] if state else "dim") for t in _wrap(" · ".join(parts), width, "  ")]
    if pilot["state"] in ("blocked", "stalled") and pilot.get("note"):
        lines += [Line(t, "dim") for t in _wrap(pilot["note"], width, "  ")]
    u = pilot.get("usage")
    if u and u["used"] >= 75:
        from copse.autopilot import usage_note

        lines += [Line(t, "alert") for t in _wrap(usage_note(u), width, "  ")]
    return lines + [Line("")]


def render(snap: list[dict], now: float, width: int = 80, pilot: dict | None = None) -> list[Line]:
    agents_ = [a for ws in snap for a in ws["agents"]]
    lines = [Line(summary(snap), "alert" if any(a["status"] == "waiting" for a in agents_) else "bold")]
    if snap:
        lines.append(Line(f"{plural(len(agents_), 'agent')} in {plural(len(snap), 'workspace')}", "dim"))
    lines.append(Line(""))
    if pilot:
        lines += render_autopilot(pilot, width)
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
            elif a.get("headless"):
                name += " (headless)"
            lines.append(Line(f"  {icon} {name}", STATUS_STYLE.get(a["status"], "normal"),
                              agent=a, workspace=ws))
            since = a.get("status_since")
            detail = [label + (f" for {ago(now - since)}" if since else "")]
            if a.get("pending"):
                detail.append(f"{plural(a['pending'], 'message')} queued")
            detail.append(a["id"][:6])
            lines += [Line(t, "dim", workspace=ws) for t in _wrap(" · ".join(detail), width, "    ")]
            for sub in a.get("subagents") or []:
                sub_name = sub.get("agent_type") or "subagent"
                if sub["ended_at"] is None:
                    text, style = f"↳ {sub_name} · running {ago(now - sub['started_at'])}", "busy"
                else:
                    text, style = f"↳ {sub_name} · ✓ done", "dim"
                # Not selectable: no `agent=`, so it can't be attached to or peeked.
                lines += [Line(t, style, workspace=ws) for t in _wrap(text, width, "    ")]
        lines.append(Line(""))
    return lines


# -- --once ------------------------------------------------------------------

ANSI = {"bold": "1", "dim": "2", "busy": "36", "ok": "32", "alert": "1;33", "bad": "31",
        "accent": "1;35"}


def print_once(db: DB, repo_root: str | None, color: bool) -> str:
    out = []
    width = shutil.get_terminal_size().columns - 1
    for line in render(view.snapshot(db, repo_root), time.time(), width,
                       view.autopilot_entry(db, repo_root)):
        code = ANSI.get(line.style) if color else None
        out.append(f"\033[{code}m{line.text}\033[0m" if code else line.text)
    return "\n".join(out)


# -- interactive ---------------------------------------------------------------

HELP = ["↑↓ ⏎ open  p peek  q quit"]
HELP_IN_TMUX = [*HELP, "prefix L: back from agent"]


# PawDelta palette as xterm-256 colours (closest matches): indigo accent,
# soft green / amber / rose for states, slate greys for secondary text.
PALETTE_256 = {
    "accent": 105,    # ~#818cf8 indigo-light
    "busy": 141,      # soft purple: working
    "ok": 79,         # soft green: idle / done
    "alert": 215,     # amber: needs you
    "bad": 174,       # dusty rose: stopped
    "dim": 245,       # slate
    "trunk": 94,      # brown: the logo's trunk
    "text": 255,
    "select_bg": 237, # subtle row highlight
}


def _styles() -> dict[str, int]:
    attrs = {"normal": curses.A_NORMAL, "bold": curses.A_BOLD, "dim": curses.A_DIM,
             "accent": curses.A_BOLD, "select": curses.A_REVERSE, "bar": curses.A_BOLD}
    if not curses.has_colors():
        attrs.update(busy=curses.A_NORMAL, ok=curses.A_NORMAL,
                     alert=curses.A_BOLD, bad=curses.A_DIM)
        return attrs
    curses.use_default_colors()
    if curses.COLORS >= 256:
        p = PALETTE_256
        pairs = [("accent", p["accent"], -1), ("busy", p["busy"], -1), ("ok", p["ok"], -1),
                 ("alert", p["alert"], -1), ("bad", p["bad"], -1), ("dim", p["dim"], -1),
                 ("normal", p["text"], -1), ("select", p["text"], p["select_bg"]),
                 ("trunk", p["trunk"], -1),
                 ("bar", p["accent"], p["select_bg"])]
    else:
        pairs = [("accent", curses.COLOR_MAGENTA, -1), ("busy", curses.COLOR_MAGENTA, -1),
                 ("ok", curses.COLOR_GREEN, -1), ("alert", curses.COLOR_YELLOW, -1),
                 ("bad", curses.COLOR_RED, -1), ("dim", -1, -1), ("normal", -1, -1),
                 ("select", curses.COLOR_WHITE, curses.COLOR_BLUE),
                 ("trunk", curses.COLOR_YELLOW, -1),
                 ("bar", curses.COLOR_MAGENTA, curses.COLOR_BLUE)]
    for i, (name, fg, bg) in enumerate(pairs, start=1):
        curses.init_pair(i, fg, bg)
        attrs[name] = curses.color_pair(i)
    attrs["accent"] |= curses.A_BOLD
    attrs["bold"] = attrs["normal"] | curses.A_BOLD
    attrs["alert"] |= curses.A_BOLD
    attrs["bar"] |= curses.A_BOLD
    if curses.COLORS < 256:
        attrs["dim"] |= curses.A_DIM
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


LOGO = [  # one solid pine; the wordmark sits beside its widest row
    ("   ◢◣", ""),
    ("  ◢██◣", ""),
    (" ◢████◣  ", "copse"),
    ("   ██", ""),
]


def _draw_logo(stdscr, w: int, styles: dict[str, int]) -> int:
    """The pine-and-wordmark header. Returns the first free row."""
    clock = time.strftime("%H:%M")
    for y, (tree, word) in enumerate(LOGO):
        trunk = y == len(LOGO) - 1
        stdscr.addnstr(y, 1, tree, w - 2, styles.get("trunk", styles["accent"]) if trunk else styles["accent"])
        if word and w > len(tree) + len(word) + 2:
            stdscr.addnstr(y, 1 + len(tree), word, len(word), styles["bold"])
    if w > len(clock) + 16:
        stdscr.addnstr(0, w - 1 - len(clock), clock, len(clock), styles["dim"])
    stdscr.addnstr(len(LOGO), 1, "─" * max(0, w - 3), w - 2, styles["dim"])
    return len(LOGO) + 1


def _draw_compact_logo(stdscr, w: int, styles: dict[str, int]) -> int:
    """One-line header for short panes."""
    stdscr.addnstr(0, 1, "◢◣", w - 2, styles["accent"])
    stdscr.addnstr(0, 4, "copse", max(0, w - 5), styles["bold"])
    stdscr.addnstr(1, 1, "─" * max(0, w - 3), w - 2, styles["dim"])
    return 2


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
            lines = render(view.snapshot(db, repo_root), time.time(), w - 1,
                           view.autopilot_entry(db, repo_root))
            stale = False
        rows = [i for i, ln in enumerate(lines) if ln.agent]
        selected = max(0, min(selected, len(rows) - 1))

        help_ = [t for text in (HELP_IN_TMUX if os.environ.get("TMUX") else HELP)
                 for t in _wrap(text, w - 1, "")]
        stdscr.erase()
        top = _draw_logo(stdscr, w, styles) if h >= 18 else _draw_compact_logo(stdscr, w, styles)
        for y, (i, ln) in enumerate(enumerate(lines[: h - top - 1 - len(help_)]), start=top):
            attr = styles.get(ln.style, curses.A_NORMAL)
            if rows and i == rows[selected]:
                # A purple bar and a subtle highlight, not inverted colours.
                stdscr.addnstr(y, 0, "▌", 1, styles["bar"])
                stdscr.addnstr(y, 1, ln.text.ljust(w - 2), w - 2,
                               styles["select"] | (attr & curses.A_BOLD))
                continue
            stdscr.addnstr(y, 1, ln.text, w - 2, attr)
        for y, text in enumerate(help_, start=h - len(help_)):
            stdscr.addnstr(y, 1, text, w - 2, styles["dim"])
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
