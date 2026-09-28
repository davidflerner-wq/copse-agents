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
    assert lines[i].style == "alert" and lines[i].text.startswith("  ◆ ")
    assert lines[i + 1].text.strip().startswith("needs you for 1m")
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


def test_clamp_scroll_keeps_offset_in_bounds():
    assert watch.clamp_scroll(0, total=10, visible=5) == 0
    assert watch.clamp_scroll(-3, total=10, visible=5) == 0
    assert watch.clamp_scroll(100, total=10, visible=5) == 5
    # content that fits entirely: no scrolling at all
    assert watch.clamp_scroll(3, total=4, visible=5) == 0


def test_clamp_scroll_reclamps_when_content_shrinks():
    # e.g. an agent finished and its lines disappeared from the render
    assert watch.clamp_scroll(20, total=10, visible=5) == 5
    assert watch.clamp_scroll(20, total=3, visible=5) == 0


def test_scroll_into_view_scrolls_down_to_reveal_a_later_selection():
    assert watch.scroll_into_view(0, index=12, visible=5, total=20) == 8


def test_scroll_into_view_scrolls_up_to_reveal_an_earlier_selection():
    assert watch.scroll_into_view(10, index=2, visible=5, total=20) == 2


def test_scroll_into_view_leaves_offset_when_selection_already_visible():
    assert watch.scroll_into_view(4, index=6, visible=5, total=20) == 4


def test_content_layout_shows_no_indicators_when_everything_fits():
    rows, above, below = watch.content_layout(total=10, height=10, offset=0)
    assert (rows, above, below) == (10, False, False)


def test_content_layout_reserves_a_row_for_more_below():
    rows, above, below = watch.content_layout(total=11, height=10, offset=0)
    assert (rows, above, below) == (9, False, True)


def test_content_layout_reserves_rows_for_both_indicators():
    rows, above, below = watch.content_layout(total=20, height=10, offset=5)
    assert (rows, above, below) == (8, True, True)


def test_content_layout_only_more_above_when_scrolled_to_the_end():
    rows, above, below = watch.content_layout(total=15, height=10, offset=6)
    assert (rows, above, below) == (9, True, False)


def test_nearest_visible_row_prefers_a_row_already_on_the_new_page():
    assert watch.nearest_visible_row([2, 8, 15], offset=5, visible=10) == 1  # row 8


def test_nearest_visible_row_falls_back_to_the_closest_row_when_none_are_visible():
    assert watch.nearest_visible_row([2, 30], offset=10, visible=5) == 0
    assert watch.nearest_visible_row([2, 30], offset=25, visible=5) == 1


def test_nearest_visible_row_with_no_agents_is_zero():
    assert watch.nearest_visible_row([], offset=5, visible=10) == 0


def test_scroll_into_view_clamps_to_content_bounds():
    assert watch.scroll_into_view(0, index=19, visible=5, total=20) == 15
    assert watch.scroll_into_view(0, index=0, visible=5, total=3) == 0


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


def test_old_databases_get_the_sidebars_table(tmp_path):
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
    assert db.get_sidebar_pane("a") is None  # table exists and is queryable
    db.set_sidebar_pane("a", "%1")
    assert db.get_sidebar_pane("a") == "%1"


def test_older_copse_ignores_columns_from_a_newer_one(tmp_path):
    # A newer copse may add columns to ~/.copse/copse.db; this version must
    # still read the rows instead of crashing (as 0.1.x did on 0.2.0's).
    from copse.db import Agent
    db = DB(str(tmp_path / "t.db"))
    db.conn.execute("INSERT INTO workspaces VALUES ('w','/r','n','main','b',NULL,'/p',NULL,'s',0)")
    db.add_agent(Agent("a", "w", "developer", "claude", None, "interactive", "idle", "@1", None, 1.0))
    db.conn.execute("ALTER TABLE agents ADD COLUMN from_the_future TEXT")
    db.conn.execute("ALTER TABLE workspaces ADD COLUMN also_new INTEGER")
    assert db.get_agent("a").status == "idle"
    assert [a.id for a in db.list_agents("w")] == ["a"]
    assert db.get_workspace("w").name == "n"
