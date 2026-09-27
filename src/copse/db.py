"""SQLite state shared by the CLI, hook handlers, and every agent's MCP server.

Several processes touch the database at once (one MCP server per agent plus
hook invocations), so it runs in WAL mode and every write is a short
transaction.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
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
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sender_id TEXT,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL
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


@dataclass
class Message:
    id: int
    agent_id: str
    sender_id: str | None
    body: str
    created_at: float
    delivered_at: float | None


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
        return Workspace(**row) if row else None

    def find_workspaces(self, repo_root: str | None = None) -> list[Workspace]:
        if repo_root:
            rows = self.conn.execute(
                "SELECT * FROM workspaces WHERE repo_root=? ORDER BY created_at", (repo_root,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM workspaces ORDER BY created_at")
        return [Workspace(**r) for r in rows]

    def workspace_by_path(self, path: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE path=?", (path,)).fetchone()
        return Workspace(**row) if row else None

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
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.workspace_id, a.profile, a.provider, a.parent_id, a.mode,
                 a.status, a.tmux_window, a.result, a.created_at),
            )

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return Agent(**row) if row else None

    def list_agents(self, workspace_id: str | None = None) -> list[Agent]:
        if workspace_id:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE workspace_id=? ORDER BY created_at", (workspace_id,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY created_at")
        return [Agent(**r) for r in rows]

    def children(self, parent_id: str) -> list[Agent]:
        rows = self.conn.execute(
            "SELECT * FROM agents WHERE parent_id=? ORDER BY created_at", (parent_id,)
        )
        return [Agent(**r) for r in rows]

    def update_agent(self, agent_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))

    def claim_idle(self, agent_id: str) -> bool:
        """Atomically flip idle -> processing. Only the caller that wins may type
        into the agent's terminal, so two senders never interleave keystrokes."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE agents SET status='processing' WHERE id=? AND status='idle'", (agent_id,)
            )
            return cur.rowcount == 1

    def set_status(self, agent_id: str, status: str, only_if: str | None = None) -> None:
        if only_if is not None:
            with self.tx() as c:
                c.execute(
                    "UPDATE agents SET status=? WHERE id=? AND status=?",
                    (status, agent_id, only_if),
                )
            return
        with self.tx() as c:
            c.execute("UPDATE agents SET status=? WHERE id=?", (status, agent_id))

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
            return Message(**row)

    def drop_pending(self, agent_id: str, sender_id: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM inbox WHERE agent_id=? AND sender_id=? AND delivered_at IS NULL",
                (agent_id, sender_id),
            )
            return cur.rowcount

    def pending_count(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND delivered_at IS NULL", (agent_id,)
        ).fetchone()
        return int(row[0])
