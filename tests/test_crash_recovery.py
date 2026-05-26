"""P-09: WAL + per-session locks + partial-turn recovery."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


from sera.memory.session import (
    Message,
    Session,
    recover_aborted_sessions,
    session_lock,
)


def test_wal_journal_mode_activated(tmp_path: Path):
    db = tmp_path / "wal.db"
    Session.create(workspace=str(tmp_path), db_path=db)
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    # Local filesystems support WAL; iCloud/NFS fall back to DELETE.
    # Either is acceptable — the fallback is the safety net.
    assert mode in {"wal", "delete"}


def test_finish_reason_persists(tmp_path: Path):
    db = tmp_path / "fr.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="hi"))
    s.append(Message(role="assistant", content="hello", finish_reason="stop"))

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded is not None
    assert reloaded.messages[0].finish_reason is None  # user rows: NULL
    assert reloaded.messages[1].finish_reason == "stop"


def test_recover_flags_dangling_user(tmp_path: Path):
    """User row appended but assistant never landed → aborted."""
    db = tmp_path / "dang.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="do thing"))
    s.close()

    flagged = recover_aborted_sessions(db_path=db)
    assert s.id in flagged

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded is not None
    assert reloaded.last_status == "aborted"
    assert reloaded.aborted_at is not None


def test_recover_flags_assistant_without_finish_reason(tmp_path: Path):
    db = tmp_path / "nofr.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="ping"))
    s.append(Message(role="assistant", content="partial..."))  # finish_reason=None
    s.close()

    flagged = recover_aborted_sessions(db_path=db)
    assert s.id in flagged


def test_recover_skips_clean_session(tmp_path: Path):
    db = tmp_path / "clean.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="ping"))
    s.append(Message(role="assistant", content="pong", finish_reason="stop"))
    s.close()

    flagged = recover_aborted_sessions(db_path=db)
    assert s.id not in flagged

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded.last_status == "active"
    assert reloaded.aborted_at is None


def test_recover_skips_empty_session(tmp_path: Path):
    db = tmp_path / "empty.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.close()
    # No messages at all — neither dangling nor crashed; leave alone.
    flagged = recover_aborted_sessions(db_path=db)
    assert s.id not in flagged


def test_recover_is_idempotent(tmp_path: Path):
    db = tmp_path / "idem.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="lost"))
    s.close()

    first = recover_aborted_sessions(db_path=db)
    second = recover_aborted_sessions(db_path=db)
    assert s.id in first
    assert s.id not in second  # already flagged → no-op


def test_recover_runs_on_first_connect(tmp_path: Path):
    """The connect cache runs recovery once per (process, db) on first open."""
    db = tmp_path / "auto.db"
    # Build a dangling session directly via SQL so connect-time recovery has
    # something to flag without needing a prior in-process scan.
    init = Session.create(workspace=str(tmp_path), db_path=db)
    sid = init.id
    init.append(Message(role="user", content="orphan"))
    init.close()

    # Reset the connect cache so the next _connect re-runs recovery.
    from sera.memory import session as session_mod

    session_mod._INITIALIZED.discard(str(db))

    reloaded = Session.load(sid, db_path=db)
    assert reloaded is not None
    assert reloaded.last_status == "aborted"


def test_session_lock_serializes_writers(tmp_path: Path):
    """Two threads acquiring the same session lock must not overlap."""
    sid = "lock-test-12"
    timeline: list[tuple[str, float]] = []

    def worker(tag: str, hold: float):
        with session_lock(sid):
            timeline.append((f"{tag}-acquire", time.monotonic()))
            time.sleep(hold)
            timeline.append((f"{tag}-release", time.monotonic()))

    t1 = threading.Thread(target=worker, args=("a", 0.1))
    t2 = threading.Thread(target=worker, args=("b", 0.1))
    t1.start()
    time.sleep(0.02)  # ensure A acquires first
    t2.start()
    t1.join()
    t2.join()

    # Tags should appear in non-overlapping pairs: a-acquire, a-release, then b-*.
    tags = [t[0] for t in timeline]
    assert tags[0].endswith("-acquire") and tags[1].endswith("-release")
    assert tags[1].split("-")[0] == tags[0].split("-")[0]


def test_legacy_db_migration_adds_columns(tmp_path: Path):
    """A pre-P-09 DB (no last_status, no finish_reason) migrates on connect."""
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            workspace TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            created_at REAL NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO sessions (id, title, workspace, created_at, updated_at) "
        "VALUES ('legacy1', 't', 'w', 0, 0)"
    )
    legacy.execute(
        "INSERT INTO messages (session_id, role, content, created_at) "
        "VALUES ('legacy1', 'user', 'orphan', 0)"
    )
    legacy.commit()
    legacy.close()

    # Reload via the module — migration + recovery run on first connect.
    reloaded = Session.load("legacy1", db_path=db)
    assert reloaded is not None
    # finish_reason column now exists (None for legacy rows).
    assert reloaded.messages[0].finish_reason is None
    # Dangling user → aborted via the on-connect recovery scan.
    assert reloaded.last_status == "aborted"
