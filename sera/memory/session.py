"""SQLite session store with FTS5. Heritage: hermes/hermes_state.py:1-120."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sera.config import SESSIONS_DB, ensure_home

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    workspace TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, role, content='messages', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, role)
    VALUES (new.id, COALESCE(new.content, ''), new.role);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, role)
    VALUES ('delete', old.id, COALESCE(old.content, ''), old.role);
END;
"""


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        m: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            m["content"] = self.content
        if self.tool_calls:
            m["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            m["tool_call_id"] = self.tool_call_id
        if self.name:
            m["name"] = self.name
        return m


# Cache the per-path "schema applied" flag so we run the DDL exactly once per
# database file. Eliminates ~30 PRAGMA + CREATE round-trips per `append`.
_INITIALIZED: set[str] = set()


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a new connection. Use sparingly — prefer `Session._conn`.

    Used by CLI commands that need to query without owning a Session
    (e.g. `sera sessions` listing).
    """
    ensure_home()
    target = path or SESSIONS_DB
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    key = str(target)
    if key not in _INITIALIZED:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _INITIALIZED.add(key)
    return conn


class Session:
    """In-memory + SQLite-backed conversation state.

    Holds a single persistent `sqlite3.Connection` for the session's lifetime;
    `append()` reuses it instead of opening a new connection per write.
    """

    def __init__(
        self,
        session_id: str,
        workspace: str,
        title: str = "",
        db_path: Path | None = None,
    ) -> None:
        self.id = session_id
        self.workspace = workspace
        self.title = title
        self.messages: list[Message] = []
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @classmethod
    def create(cls, workspace: str, title: str = "", db_path: Path | None = None) -> "Session":
        sid = uuid.uuid4().hex[:12]
        s = cls(sid, workspace=workspace, title=title or "untitled", db_path=db_path)
        now = time.time()
        s.conn.execute(
            "INSERT INTO sessions (id, title, workspace, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (s.id, s.title, s.workspace, now, now),
        )
        s.conn.commit()
        return s

    @classmethod
    def load(cls, session_id: str, db_path: Path | None = None) -> "Session | None":
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, title, workspace FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                conn.close()
                return None
            s = cls(
                row["id"],
                workspace=row["workspace"] or "",
                title=row["title"] or "",
                db_path=db_path,
            )
            # Reuse the opened connection rather than discarding it.
            s._conn = conn
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, name FROM messages "
                "WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            for r in rows:
                s.messages.append(
                    Message(
                        role=r["role"],
                        content=r["content"],
                        tool_calls=json.loads(r["tool_calls"]) if r["tool_calls"] else [],
                        tool_call_id=r["tool_call_id"],
                        name=r["name"],
                    )
                )
            return s
        except Exception:
            conn.close()
            raise

    def append(self, msg: Message) -> None:
        self.messages.append(msg)
        now = time.time()
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.id,
                msg.role,
                msg.content,
                json.dumps(msg.tool_calls) if msg.tool_calls else None,
                msg.tool_call_id,
                msg.name,
                now,
            ),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, self.id),
        )
        self.conn.commit()

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        current_only: bool = False,
    ) -> list[tuple[str, str]]:
        """FTS5 search. Returns [(role, snippet), ...].

        current_only=True scopes to this session; else cross-session.
        Query is escaped to a single FTS5 phrase so colons/quotes don't crash.
        """
        match_expr = _escape_fts5(query)
        sql = (
            "SELECT m.role, snippet(messages_fts, 0, '[', ']', '...', 16) AS snip "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ?"
        )
        params: list = [match_expr]
        if current_only:
            sql += " AND m.session_id = ?"
            params.append(self.id)
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [(r["role"], r["snip"]) for r in rows]


def _escape_fts5(query: str) -> str:
    """Wrap user input as a single FTS5 phrase, escaping embedded quotes.

    FTS5 has its own MATCH grammar (operators like :, AND, NOT, *). Quoting
    the whole input forces phrase-mode and dodges syntax errors.
    """
    q = (query or "").strip()
    if not q:
        return '""'
    return '"' + q.replace('"', '""') + '"'
