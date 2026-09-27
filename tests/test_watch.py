import sqlite3
import time

from copse import watch
from copse.db import DB


def ws(agents, **kw):
    return {"id": "repo/feat", "name": "feat", "branch": "feat", "base_branch": "main",
            "path": "/", "ahead": 2, "behind": 1, "dirty": 3, "agents": agents, **kw}


def agent(status, **kw):
    return {"id": "a1b2c3d4", "profile": "developer", "provider": "claude", "status": status,
            "mode": "assign", "status_since": 1000.0, "pending": 0, "reported": False,
            "window": "@1", **kw}


def test_waiting_agents_stand_out():
    lines = watch.render([ws([agent("waiting"), agent("processing", id="e5f6")])], now=1090)
    assert lines[0].text == "1 needs you · 1 working" and lines[0].style == "alert"
    assert lines[1].text == "2 agents in 1 workspace"
    i = next(i for i, ln in enumerate(lines) if ln.agent and ln.agent["status"] == "waiting")
    assert lines[i].style == "alert" and "◆ Developer" in lines[i].text
    assert lines[i + 1].text.strip().startswith("needs you 1m")
    assert any("2 ahead · 1 behind main · 3 files changed" in ln.text for ln in lines)


def test_lines_fit_a_narrow_sidebar():
    lines = watch.render([ws([agent("processing", pending=3)])], now=1090, width=30)
    assert all(len(ln.text) <= 30 for ln in lines)


def test_queued_messages_and_reports_are_shown():
    lines = watch.render([ws([agent("idle", pending=2, reported=True)])], now=1005)
    i = next(i for i, ln in enumerate(lines) if ln.agent)
    assert lines[i].style == "ok" and "✓ Developer" in lines[i].text
    assert "done" in lines[i + 1].text and "2 messages queued" in lines[i + 1].text


def test_empty_state():
    lines = watch.render([], now=0)
    assert "Nothing running yet" in lines[-1].text


def test_ago():
    assert [watch.ago(s) for s in (5, 125, 3720, 90000)] == ["5s", "2m", "1h02m", "1d"]


def test_status_since_only_moves_on_change(tmp_path, monkeypatch):
    from copse.db import Agent
    db = DB(str(tmp_path / "t.db"))
    db.conn.execute("INSERT INTO workspaces VALUES ('w','/r','n','main','b',NULL,'/p',NULL,'s',0)")
    db.add_agent(Agent("a", "w", "developer", "claude", None, "interactive", "idle", "@1", None, 1.0))
    monkeypatch.setattr(time, "time", lambda: 50.0)
    db.set_status("a", "idle")
    assert db.get_agent("a").status_since == 1.0
    db.set_status("a", "processing")
    assert db.get_agent("a").status_since == 50.0


def test_old_databases_are_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE agents (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, profile TEXT NOT NULL,
          provider TEXT NOT NULL, parent_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
          tmux_window TEXT NOT NULL, result TEXT, created_at REAL NOT NULL);
        INSERT INTO agents VALUES ('a','w','p','claude',NULL,'assign','idle','@1',NULL,7.0);
    """)
    con.commit(); con.close()
    db = DB(str(path))
    a = db.get_agent("a")
    assert a.status_since is None and a.status == "idle"
    db.set_status("a", "processing")
    assert db.get_agent("a").status_since is not None


def test_workers_hang_off_their_supervisor_as_branches():
    root = ws([agent("idle", id="boss0001", profile="supervisor", mode="interactive")],
              id="repo/root", name="root", branch="main", ahead=None)
    w1 = ws([agent("processing", id="w1aaaaaa", parent_id="boss0001")], id="repo/a", branch="feat/csv")
    w2 = ws([agent("waiting", id="w2bbbbbb", parent_id="boss0001")], id="repo/b", branch="feat/settings")
    text = [ln.text for ln in watch.render([root, w1, w2], now=1090, width=30)]
    assert text[3] == "main  (your checkout)"
    assert text[4].startswith("─┬─○ Supervisor")
    assert any(t.startswith("  ├─● feat/csv") for t in text)
    assert any(t.startswith("  └─◆ feat/settings") for t in text)
    assert all(len(t) <= 30 for t in text)
