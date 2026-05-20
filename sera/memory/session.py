"""SQLite session store with FTS5 + crash recovery. Heritage: hermes/hermes_state.py:1-120.

P-09 additions:

- WAL journal mode with `DELETE` fallback when the host filesystem rejects
  WAL (NFS, iCloud Drive in some configurations, sshfs). Probe by writing
  the pragma and reading back the active mode.
- Per-session advisory lock via `fcntl.flock` so concurrent `sera` processes
  serialize on the same session_id without stomping each other's commits.
- Explicit partial-turn recovery: on first connect, scan every session
  whose last message is either a dangling `user` row (mid-stream crash)
  or an `assistant` row with NULL `finish_reason`. Flip the session's
  `last_status` to `aborted` and stamp `aborted_at`. The CLI's
  `sera sessions` view surfaces the flag.

Outclass: Hermes ships WAL, none ship explicit partial-turn detection. Sera
turns a kill -9 mid-tool into a recovered session with a visible abort flag.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sera.config import SERA_HOME, SESSIONS_DB, ensure_home

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    workspace TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'active',
    aborted_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    finish_reason TEXT,
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
    finish_reason: str | None = None

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
_INIT_LOCK = threading.Lock()


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
        # Serialize first-time init across threads in the same process — two
        # concurrent connects against a fresh DB would otherwise race on the
        # ALTERs and recovery scan.
        with _INIT_LOCK:
            if key not in _INITIALIZED:
                conn.executescript(_SCHEMA)
                mode = _set_journal_mode(conn)
                if mode != "wal":
                    _warn_wal_fallback_once(key, mode)
                conn.execute("PRAGMA synchronous=NORMAL")
                _migrate_columns(conn, "sessions", _SESSIONS_COLUMNS_TO_ADD)
                _migrate_columns(conn, "messages", _MESSAGES_COLUMNS_TO_ADD)
                _recover_aborted(conn)
                _INITIALIZED.add(key)
    return conn


# ─── Per-session advisory lock ────────────────────────────────────────────

_LOCKS_DIR = SERA_HOME / "locks"


def _lock_path(session_id: str) -> Path:
    return _LOCKS_DIR / f"{session_id}.lock"


@contextmanager
def session_lock(session_id: str) -> Iterator[None]:
    """Advisory exclusive lock per session_id for the body of the `with`.

    Implemented with `fcntl.flock` on a per-session lockfile under
    `~/.sera/locks/`. Two `sera` processes editing the same session_id
    will serialize — the second blocks until the first releases.

    No-op when `fcntl` is unavailable (Windows). On Windows, SQLite's
    own file-level lock still serializes concurrent writers; the
    per-session granularity is lost but correctness is preserved.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(session_id)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ─── Crash recovery ───────────────────────────────────────────────────────


def _recover_aborted(conn: sqlite3.Connection) -> list[str]:
    """Flag sessions whose last turn was interrupted.

    A session is considered aborted if its most recent message is either
    a user row (mid-stream crash before the assistant reply landed) or
    an assistant row with NULL `finish_reason` (the loop crashed after
    inserting the row but before stamping the reason).

    Idempotent: skips sessions already flagged `aborted` and only flips
    `active` rows. Returns the list of session ids freshly flagged.
    """
    rows = conn.execute(
        "SELECT s.id, m.role, m.finish_reason FROM sessions s "
        "LEFT JOIN messages m ON m.id = ("
        "  SELECT id FROM messages WHERE session_id = s.id "
        "  ORDER BY id DESC LIMIT 1"
        ") "
        "WHERE s.last_status = 'active'"
    ).fetchall()
    flagged: list[str] = []
    now = time.time()
    for r in rows:
        last_role = r["role"]
        if last_role is None:
            continue  # no messages → nothing to recover; leave 'active'
        finish = r["finish_reason"]
        dangling = (
            last_role == "user"
            or (last_role == "assistant" and finish is None)
        )
        if dangling:
            conn.execute(
                "UPDATE sessions SET last_status = 'aborted', aborted_at = ? "
                "WHERE id = ? AND last_status = 'active'",
                (now, r["id"]),
            )
            flagged.append(r["id"])
    if flagged:
        conn.commit()
        logger.info("crash-recovery: flagged %d session(s) as aborted", len(flagged))
    return flagged


def recover_aborted_sessions(db_path: Path | None = None) -> list[str]:
    """Public entry: run the recovery scan against the named DB and return
    the freshly-flagged session ids. Exposed so the CLI can re-run scans
    on demand without restarting the process.
    """
    conn = _connect(db_path)
    try:
        return _recover_aborted(conn)
    finally:
        conn.close()


_SESSIONS_COLUMNS_TO_ADD: tuple[tuple[str, str], ...] = (
    ("system_prompt", "TEXT"),
    ("system_prompt_hash", "TEXT"),
    ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("last_status", "TEXT NOT NULL DEFAULT 'active'"),
    ("aborted_at", "REAL"),
)

_MESSAGES_COLUMNS_TO_ADD: tuple[tuple[str, str], ...] = (
    ("finish_reason", "TEXT"),
)


def _migrate_columns(
    conn: sqlite3.Connection, table: str, additions: tuple[tuple[str, str], ...]
) -> None:
    """Add missing columns to `table`. Idempotent — re-runs are no-ops.

    Sqlite lacks `ALTER TABLE ADD COLUMN IF NOT EXISTS`; introspect via
    PRAGMA and only add the missing columns.
    """
    existing = {
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, decl in additions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def _migrate_sessions_columns(conn: sqlite3.Connection) -> None:
    """Legacy alias retained for any external callers."""
    _migrate_columns(conn, "sessions", _SESSIONS_COLUMNS_TO_ADD)


def _set_journal_mode(conn: sqlite3.Connection) -> str:
    """Try WAL; fall back to DELETE if the filesystem rejects WAL.

    SQLite's WAL mode requires the host to support shared memory and OS
    file locking that WAL relies on. On NFS, certain iCloud configs, and
    a few sshfs setups, the PRAGMA reports back the prior mode instead
    of switching. Returning the *effective* mode lets the caller log a
    one-time warning if WAL didn't take.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    mode_row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = (mode_row[0] if mode_row else "").lower()
    if mode != "wal":
        conn.execute("PRAGMA journal_mode=DELETE")
        fallback_row = conn.execute("PRAGMA journal_mode").fetchone()
        return (fallback_row[0] if fallback_row else "delete").lower()
    return mode


_WAL_WARNED: set[str] = set()


def _warn_wal_fallback_once(path: str, mode: str) -> None:
    if path in _WAL_WARNED:
        return
    _WAL_WARNED.add(path)
    logger.warning(
        "sessions db at %s rejected WAL; falling back to journal_mode=%s. "
        "Crash recovery still works but concurrent writers will serialize harder.",
        path,
        mode,
    )


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
        last_status: str = "active",
        aborted_at: float | None = None,
    ) -> None:
        self.id = session_id
        self.workspace = workspace
        self.title = title
        self.last_status = last_status
        self.aborted_at = aborted_at
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
                "SELECT id, title, workspace, last_status, aborted_at "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.close()
                return None
            s = cls(
                row["id"],
                workspace=row["workspace"] or "",
                title=row["title"] or "",
                db_path=db_path,
                last_status=row["last_status"] or "active",
                aborted_at=row["aborted_at"],
            )
            # Reuse the opened connection rather than discarding it.
            s._conn = conn
            rows = conn.execute(
                "SELECT role, content, tool_calls, tool_call_id, name, finish_reason "
                "FROM messages WHERE session_id = ? ORDER BY id ASC",
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
                        finish_reason=r["finish_reason"],
                    )
                )
            return s
        except Exception:
            conn.close()
            raise

    def append(self, msg: Message) -> None:
        """Persist a message under an exclusive per-session lock.

        The lock serializes concurrent `sera` processes editing the same
        session_id. It does NOT serialize unrelated sessions — `flock`
        granularity is per lockfile.

        Side effects: appends to `self.messages`, inserts into `messages`,
        bumps `sessions.updated_at`. Persists `finish_reason` if set on the
        message — that field is the recovery scan's signal for "this
        assistant turn completed cleanly". Assistant rows persisted with
        NULL `finish_reason` will be flagged `aborted` on next startup.
        """
        self.messages.append(msg)
        now = time.time()
        with session_lock(self.id):
            self.conn.execute(
                "INSERT INTO messages "
                "(session_id, role, content, tool_calls, tool_call_id, name, "
                "finish_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.id,
                    msg.role,
                    msg.content,
                    json.dumps(msg.tool_calls) if msg.tool_calls else None,
                    msg.tool_call_id,
                    msg.name,
                    msg.finish_reason,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, self.id),
            )
            self.conn.commit()

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
    ) -> None:
        """Accumulate per-turn token counts onto the sessions row.

        Called once per assistant turn after the LLM stream completes.
        Sums are running totals for the lifetime of the session.
        """
        self.conn.execute(
            "UPDATE sessions SET "
            "input_tokens = input_tokens + ?, "
            "output_tokens = output_tokens + ?, "
            "cache_read_tokens = cache_read_tokens + ?, "
            "cache_creation_tokens = cache_creation_tokens + ? "
            "WHERE id = ?",
            (
                int(input_tokens),
                int(output_tokens),
                int(cache_read_tokens),
                int(cache_creation_tokens),
                self.id,
            ),
        )
        self.conn.commit()

    def usage_totals(self) -> dict[str, int]:
        """Read back the running totals for this session."""
        row = self.conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens FROM sessions WHERE id = ?",
            (self.id,),
        ).fetchone()
        if row is None:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            }
        return {k: int(row[k] or 0) for k in row.keys()}

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
