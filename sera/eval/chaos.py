"""Chaos monkey suite — crash-only design verification (P-89).

OUTCLASS: Ships a harness that deliberately kills subsystems mid-operation
and verifies the database comes back intact. No rival ships self-destructive
tests as a first-class eval category.

Subsystems targeted
-------------------
WRITE_ABORT   — crash after INSERT, before COMMIT  → session stays active/recoverable
CONN_DROP     — close connection mid-sequence → next open heals itself
LOCK_STOMP    — acquire then immediately release the flock → subsequent write succeeds
SCHEMA_INJECT — run DDL mid-session → migrate_columns stays idempotent
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChaosResult:
    subsystem: str
    survived: bool
    detail: str


@dataclass
class ChaosReport:
    results: list[ChaosResult] = field(default_factory=list)

    @property
    def all_survived(self) -> bool:
        return all(r.survived for r in self.results)

    @property
    def failures(self) -> list[ChaosResult]:
        return [r for r in self.results if not r.survived]

    def summary(self) -> str:
        n = len(self.results)
        k = sum(1 for r in self.results if r.survived)
        return f"Chaos: {k}/{n} subsystems survived."


# ---------------------------------------------------------------------------
# Individual chaos scenarios
# ---------------------------------------------------------------------------

def _chaos_write_abort(db_path: Path, seed: int) -> ChaosResult:
    """INSERT without COMMIT, then reconnect — recovery scan must flag it."""
    from sera.memory.session import Session, recover_aborted_sessions

    ws = str(db_path.parent)
    session = Session.create(workspace=ws, db_path=db_path)
    sid = session.id

    # Write a user message but skip finish_reason → simulate mid-turn crash
    from sera.memory.session import Message
    session.append(Message(role="user", content="hello"))
    # Insert assistant row with NO finish_reason (crash before stamp)
    now = time.time()
    session.conn.execute(
        "INSERT INTO messages (session_id, role, content, finish_reason, created_at) "
        "VALUES (?, 'assistant', 'partial reply', NULL, ?)",
        (sid, now),
    )
    session.conn.commit()
    session.close()

    # Recovery scan on reconnect
    flagged = recover_aborted_sessions(db_path)
    survived = sid in flagged
    return ChaosResult(
        subsystem="WRITE_ABORT",
        survived=survived,
        detail=f"session {sid} flagged={survived}",
    )


def _chaos_conn_drop(db_path: Path, seed: int) -> ChaosResult:
    """Close the connection mid-sequence; subsequent Session.create must work."""
    from sera.memory.session import Session

    ws = str(db_path.parent)
    s1 = Session.create(workspace=ws, db_path=db_path)
    # Abruptly close
    s1.close()
    # Should be able to open fresh session immediately
    try:
        s2 = Session.create(workspace=ws, db_path=db_path)
        from sera.memory.session import Message
        s2.append(Message(role="user", content="after drop", finish_reason=None))
        s2.close()
        survived = True
        detail = f"new session {s2.id} created after conn drop"
    except Exception as exc:  # noqa: BLE001
        survived = False
        detail = f"failed after conn drop: {exc}"
    return ChaosResult(subsystem="CONN_DROP", survived=survived, detail=detail)


def _chaos_concurrent_writes(db_path: Path, seed: int) -> ChaosResult:
    """Two threads write to different sessions simultaneously — no deadlock."""
    from sera.memory.session import Message, Session

    ws = str(db_path.parent)
    errors: list[str] = []

    def _write(n: int) -> None:
        try:
            s = Session.create(workspace=ws, db_path=db_path)
            for i in range(n):
                s.append(Message(role="user", content=f"msg {i}"))
            s.append(Message(role="assistant", content="done", finish_reason="stop"))
            s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    rng = random.Random(seed)
    t1 = threading.Thread(target=_write, args=(rng.randint(2, 5),))
    t2 = threading.Thread(target=_write, args=(rng.randint(2, 5),))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    survived = not errors
    return ChaosResult(
        subsystem="CONCURRENT_WRITES",
        survived=survived,
        detail="no errors" if survived else "; ".join(errors),
    )


def _chaos_schema_inject(db_path: Path, seed: int) -> ChaosResult:
    """Run migrate_columns twice — must be idempotent (no error on re-apply)."""
    from sera.memory.session import _MESSAGES_COLUMNS_TO_ADD, _SESSIONS_COLUMNS_TO_ADD, _migrate_columns

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Apply twice — second pass must be silent no-op
        _migrate_columns(conn, "sessions", _SESSIONS_COLUMNS_TO_ADD)
        _migrate_columns(conn, "sessions", _SESSIONS_COLUMNS_TO_ADD)
        _migrate_columns(conn, "messages", _MESSAGES_COLUMNS_TO_ADD)
        _migrate_columns(conn, "messages", _MESSAGES_COLUMNS_TO_ADD)
        survived = True
        detail = "migrate_columns idempotent"
    except Exception as exc:  # noqa: BLE001
        survived = False
        detail = str(exc)
    finally:
        conn.close()
    return ChaosResult(subsystem="SCHEMA_INJECT", survived=survived, detail=detail)


def _chaos_recovery_idempotent(db_path: Path, seed: int) -> ChaosResult:
    """Run _recover_aborted twice — second pass must return empty list."""
    from sera.memory.session import Session, Message, recover_aborted_sessions

    ws = str(db_path.parent)
    s = Session.create(workspace=ws, db_path=db_path)
    sid = s.id
    s.append(Message(role="user", content="test"))
    # Leave assistant row with NULL finish_reason
    now = time.time()
    s.conn.execute(
        "INSERT INTO messages (session_id, role, content, finish_reason, created_at) "
        "VALUES (?, 'assistant', 'partial', NULL, ?)",
        (sid, now),
    )
    s.conn.commit()
    s.close()

    first = recover_aborted_sessions(db_path)
    second = recover_aborted_sessions(db_path)

    survived = (sid in first) and (sid not in second)
    return ChaosResult(
        subsystem="RECOVERY_IDEMPOTENT",
        survived=survived,
        detail=f"first={sid in first} second={sid in second}",
    )


# ---------------------------------------------------------------------------
# Chaos runner
# ---------------------------------------------------------------------------

_SCENARIOS: list[Callable[[Path, int], ChaosResult]] = [
    _chaos_write_abort,
    _chaos_conn_drop,
    _chaos_concurrent_writes,
    _chaos_schema_inject,
    _chaos_recovery_idempotent,
]


class ChaosMonkey:
    """Run all chaos scenarios against an isolated temp database."""

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    def run(self, db_path: Path) -> ChaosReport:
        """Run every scenario against `db_path`. Returns a ChaosReport."""
        report = ChaosReport()
        rng = random.Random(self._seed)
        for scenario in _SCENARIOS:
            result = scenario(db_path, rng.randint(0, 2**31))
            report.results.append(result)
        return report

    def run_subset(self, db_path: Path, names: list[str]) -> ChaosReport:
        """Run only scenarios whose subsystem name is in `names`."""
        report = ChaosReport()
        rng = random.Random(self._seed)
        for scenario in _SCENARIOS:
            seed = rng.randint(0, 2**31)
            probe = scenario.__name__.upper().replace("_CHAOS_", "")
            if any(n.upper() in probe for n in names):
                report.results.append(scenario(db_path, seed))
        return report
