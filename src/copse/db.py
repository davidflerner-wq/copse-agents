"""SQLite state shared by the CLI, hook handlers, and every agent's MCP server.

Several processes touch the database at once (one MCP server per agent plus
hook invocations), so it runs in WAL mode and every write is a short
transaction.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Iterator

from copse.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'worktree' (copse-managed) or 'main' (existing checkout)
    branch TEXT NOT NULL,
    base_branch TEXT,
    path TEXT NOT NULL,
    port_base INTEGER,
    tmux_session TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (repo_root, name)
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    provider TEXT NOT NULL,
    parent_id TEXT,
    mode TEXT NOT NULL,            -- 'interactive', 'handoff', or 'assign'
    status TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    status_since REAL,             -- when status last changed
    task TEXT,                     -- the prompt it was started with (for resuming)
    session_ref TEXT,              -- the CLI's own session id (claude --resume)
    stop_blocked INTEGER,          -- copse's Stop hook just kept it going (for CLIs that don't say)
    headless INTEGER               -- runs `claude -p` turn by turn (agents.run_headless)
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sender_id TEXT,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL
);
-- Autopilot: one row per session (keyed by its supervisor) that has it on.
CREATE TABLE IF NOT EXISTS autopilot (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    goal TEXT,                     -- NULL until the user says what we're building
    detail TEXT,
    state TEXT NOT NULL DEFAULT 'running',  -- running | blocked | stalled | done
    note TEXT,                     -- why it's blocked or stalled
    progress INTEGER NOT NULL DEFAULT 0,    -- bumped whenever real progress happens
    nudges INTEGER NOT NULL DEFAULT 0,      -- "keep going" nudges since the last progress
    nudged_at INTEGER,             -- the progress count at the last nudge
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL REFERENCES autopilot(root_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    check_cmd TEXT,                -- copse runs this itself; exit 0 means done
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | passed | failed
    checked_at REAL,
    output TEXT,                   -- the tail of the last check's output
    checked_sha TEXT,              -- the checkout's HEAD when it was last checked
    passed_sha TEXT                -- the checkout's HEAD when it last passed
);
-- A reviewer agent's verdict on a branch at one commit. A merge gate only
-- accepts an approval of the commit it is about to merge.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    reviewer_id TEXT,
    approved INTEGER NOT NULL,
    summary TEXT,
    created_at REAL NOT NULL
);
"""


@dataclass
class Workspace:
    id: str
    repo_root: str
    name: str
    kind: str
    branch: str
    base_branch: str | None
    path: str
    port_base: int | None
    tmux_session: str
    created_at: float


@dataclass
class Agent:
    id: str
    workspace_id: str
    profile: str
    provider: str
    parent_id: str | None
    mode: str
    status: str
    tmux_window: str
    result: str | None
    created_at: float
    status_since: float | None = None
    task: str | None = None
    session_ref: str | None = None
    stop_blocked: int | None = None
    headless: int | None = None


@dataclass
class Autopilot:
    root_id: str
    enabled: int
    goal: str | None
    detail: str | None
    state: str
    note: str | None
    progress: int
    nudges: int
    nudged_at: int | None
    created_at: float


@dataclass
class Milestone:
    id: int
    root_id: str
    position: int
    title: str
    check_cmd: str | None
    detail: str | None
    status: str
    checked_at: float | None
    output: str | None
    checked_sha: str | None = None
    passed_sha: str | None = None


@dataclass
class Review:
    id: int
    workspace_id: str
    sha: str
    reviewer_id: str | None
    approved: int
    summary: str | None
    created_at: float


@dataclass
class Message:
    id: int
    agent_id: str
    sender_id: str | None
    body: str
    created_at: float
    delivered_at: float | None


def _load(cls, row):
    """Build ``cls`` from a row, ignoring columns this version doesn't know.
    A newer copse may have added columns; an older copse reading the same
    ~/.copse database must not crash on them."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


class DB:
    def __init__(self, path: str | None = None) -> None:
        p = path or str(db_path())
        if p != ":memory:":
            from pathlib import Path

            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(agents)")}
        for col, kind in (("status_since", "REAL"), ("task", "TEXT"), ("session_ref", "TEXT"),
                          ("stop_blocked", "INTEGER"), ("headless", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(milestones)")}
        for col in ("checked_sha", "passed_sha"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE milestones ADD COLUMN {col} TEXT")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- workspaces --------------------------------------------------------

    def add_workspace(self, ws: Workspace) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO workspaces VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ws.id, ws.repo_root, ws.name, ws.kind, ws.branch, ws.base_branch,
                 ws.path, ws.port_base, ws.tmux_session, ws.created_at),
            )

    def get_workspace(self, ws_id: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return _load(Workspace, row) if row else None

    def find_workspaces(self, repo_root: str | None = None) -> list[Workspace]:
        if repo_root:
            rows = self.conn.execute(
                "SELECT * FROM workspaces WHERE repo_root=? ORDER BY created_at", (repo_root,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM workspaces ORDER BY created_at")
        return [_load(Workspace, r) for r in rows]

    def workspace_by_path(self, path: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE path=?", (path,)).fetchone()
        return _load(Workspace, row) if row else None

    def delete_workspace(self, ws_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))

    def used_port_bases(self) -> set[int]:
        rows = self.conn.execute("SELECT port_base FROM workspaces WHERE port_base IS NOT NULL")
        return {r[0] for r in rows}

    # -- agents ------------------------------------------------------------

    def add_agent(self, a: Agent) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agents (id, workspace_id, profile, provider, parent_id, mode, "
                "status, tmux_window, result, created_at, status_since, task, session_ref, "
                "headless) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.workspace_id, a.profile, a.provider, a.parent_id, a.mode,
                 a.status, a.tmux_window, a.result, a.created_at,
                 a.status_since or a.created_at, a.task, a.session_ref, a.headless),
            )

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return _load(Agent, row) if row else None

    def list_agents(self, workspace_id: str | None = None) -> list[Agent]:
        if workspace_id:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE workspace_id=? ORDER BY created_at", (workspace_id,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY created_at")
        return [_load(Agent, r) for r in rows]

    def children(self, parent_id: str) -> list[Agent]:
        rows = self.conn.execute(
            "SELECT * FROM agents WHERE parent_id=? ORDER BY created_at", (parent_id,)
        )
        return [_load(Agent, r) for r in rows]

    def update_agent(self, agent_id: str, **fields: object) -> None:
        if "status" in fields:
            status = fields.pop("status")
            self.set_status(agent_id, str(status))
            if not fields:
                return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))

    def claim_idle(self, agent_id: str) -> bool:
        """Atomically flip idle -> processing. Only the caller that wins may type
        into the agent's terminal, so two senders never interleave keystrokes."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE agents SET status='processing', status_since=? WHERE id=? AND status='idle'",
                (time.time(), agent_id),
            )
            return cur.rowcount == 1

    _STAMP = "status_since = CASE WHEN status = ? THEN status_since ELSE ? END"

    def set_status(self, agent_id: str, status: str, only_if: str | None = None) -> None:
        guard, args = ("", ()) if only_if is None else (" AND status=?", (only_if,))
        with self.tx() as c:
            c.execute(
                f"UPDATE agents SET {self._STAMP}, status=? WHERE id=?{guard}",
                (status, time.time(), status, agent_id, *args),
            )

    def set_result(self, agent_id: str, result: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE agents SET result=? WHERE id=?", (result, agent_id))

    def delete_agent(self, agent_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM agents WHERE id=?", (agent_id,))

    # -- inbox -------------------------------------------------------------

    def enqueue(self, agent_id: str, body: str, sender_id: str | None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO inbox (agent_id, sender_id, body, created_at) VALUES (?,?,?,?)",
                (agent_id, sender_id, body, time.time()),
            )
            return int(cur.lastrowid)

    def pop_pending(self, agent_id: str) -> Message | None:
        """Atomically claim the oldest undelivered message, if any."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
                "ORDER BY id LIMIT 1",
                (agent_id,),
            ).fetchone()
            if not row:
                return None
            c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (time.time(), row["id"]))
            return _load(Message, row)

    def drop_pending(self, agent_id: str, sender_id: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM inbox WHERE agent_id=? AND sender_id=? AND delivered_at IS NULL",
                (agent_id, sender_id),
            )
            return cur.rowcount

    def recently_delivered(self, agent_id: str, body: str, within: float = 120.0) -> bool:
        """Whether ``body`` is a message copse delivered to the agent just now."""
        want = body.strip()
        if not want:
            return False
        rows = self.conn.execute(
            "SELECT body FROM inbox WHERE agent_id=? AND delivered_at > ?",
            (agent_id, time.time() - within),
        )
        return any(r[0].strip() == want for r in rows)

    def delivered_since(self, agent_id: str, since: float) -> bool:
        """Whether copse delivered any message to the agent after ``since``."""
        row = self.conn.execute(
            "SELECT 1 FROM inbox WHERE agent_id=? AND delivered_at > ? LIMIT 1", (agent_id, since),
        ).fetchone()
        return row is not None

    def pending_count(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND delivered_at IS NULL", (agent_id,)
        ).fetchone()
        return int(row[0])

    # -- autopilot -----------------------------------------------------------

    def get_autopilot(self, root_id: str) -> Autopilot | None:
        row = self.conn.execute("SELECT * FROM autopilot WHERE root_id=?", (root_id,)).fetchone()
        return _load(Autopilot, row) if row else None

    def add_autopilot(self, root_id: str, enabled: bool = True) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO autopilot (root_id, enabled, created_at) VALUES (?,?,?)",
                (root_id, int(enabled), time.time()),
            )

    def update_autopilot(self, root_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE autopilot SET {cols} WHERE root_id=?", (*fields.values(), root_id))

    def bump_progress(self, root_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE autopilot SET progress=progress+1, nudges=0 WHERE root_id=?", (root_id,))

    def set_milestones(self, root_id: str, items: list[tuple[str, str | None, str | None]]) -> None:
        """Replace the session's milestones with ``(title, check_cmd, detail)`` items."""
        with self.tx() as c:
            c.execute("DELETE FROM milestones WHERE root_id=?", (root_id,))
            for i, (title, check, detail) in enumerate(items, start=1):
                c.execute(
                    "INSERT INTO milestones (root_id, position, title, check_cmd, detail) "
                    "VALUES (?,?,?,?,?)",
                    (root_id, i, title, check, detail),
                )

    def milestones(self, root_id: str) -> list[Milestone]:
        rows = self.conn.execute(
            "SELECT * FROM milestones WHERE root_id=? ORDER BY position", (root_id,)
        )
        return [_load(Milestone, r) for r in rows]

    def record_check(self, milestone_id: int, passed: bool, output: str,
                     sha: str | None = None, *, passed_sha: str | None = None) -> None:
        """``passed_sha`` defaults to ``sha`` on a pass and is kept on a fail."""
        with self.tx() as c:
            c.execute(
                "UPDATE milestones SET status=?, checked_at=?, output=?, checked_sha=?, "
                "passed_sha=COALESCE(?, passed_sha) WHERE id=?",
                ("passed" if passed else "failed", time.time(), output, sha,
                 passed_sha or (sha if passed else None), milestone_id),
            )

    # -- reviews -------------------------------------------------------------

    def add_review(self, workspace_id: str, sha: str, reviewer_id: str | None,
                   approved: bool, summary: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO reviews (workspace_id, sha, reviewer_id, approved, summary, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (workspace_id, sha, reviewer_id, int(approved), summary, time.time()),
            )

    def latest_review(self, workspace_id: str, sha: str) -> Review | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? AND sha=? ORDER BY id DESC LIMIT 1",
            (workspace_id, sha),
        ).fetchone()
        return _load(Review, row) if row else None
