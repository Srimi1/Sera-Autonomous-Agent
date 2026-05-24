"""Capability emergence tracker — P-76.

Records when new tools and skills first appear in Sera's environment,
building a dated timeline of capability growth.  The nightly dream loop
(P-71) calls `record_snapshot(date, tools, skills)` after each session
batch; the log deduplicates across nights so only the first appearance
of each capability is stored.

`sera capability log` prints the timeline — ordered from the first tool
the agent ever had to the most recently emerged one.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from sera.config import SERA_HOME

CAPABILITY_DB = SERA_HOME / "capability_log.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capabilities (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cap_recorded ON capabilities(recorded_at);
"""


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityEntry:
    name: str
    kind: str          # "tool" | "skill"
    first_seen: str    # ISO date, e.g. "2026-05-24"
    recorded_at: float


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class CapabilityLog:
    """Append-only log of first-seen capabilities.  Idempotent on re-snapshot."""

    def __init__(self, db: Path | None = None, clock=None) -> None:
        import time as _t
        self._db = db or CAPABILITY_DB
        self._clock = clock or _t.time
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def record_snapshot(
        self,
        date: str,
        tools: list[str],
        skills: list[str],
    ) -> int:
        """Record new capabilities from this night's snapshot.

        Returns the count of newly recorded entries (0 if all already known).
        """
        now = self._clock()
        new = 0
        with self._conn() as con:
            for name in tools:
                cur = con.execute(
                    "INSERT OR IGNORE INTO capabilities (name, kind, first_seen, recorded_at) "
                    "VALUES (?, 'tool', ?, ?)",
                    (name, date, now),
                )
                new += cur.rowcount
            for name in skills:
                cur = con.execute(
                    "INSERT OR IGNORE INTO capabilities (name, kind, first_seen, recorded_at) "
                    "VALUES (?, 'skill', ?, ?)",
                    (name, date, now),
                )
                new += cur.rowcount
            con.commit()
        return new

    def timeline(self, kind: str | None = None) -> list[CapabilityEntry]:
        """Return all entries, oldest first."""
        q = "SELECT * FROM capabilities"
        params: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            params = (kind,)
        q += " ORDER BY recorded_at ASC"
        with self._conn() as con:
            rows = con.execute(q, params).fetchall()
        return [CapabilityEntry(
            name=r["name"], kind=r["kind"],
            first_seen=r["first_seen"], recorded_at=float(r["recorded_at"]),
        ) for r in rows]

    def count(self, kind: str | None = None) -> int:
        if kind:
            with self._conn() as con:
                return int(con.execute(
                    "SELECT COUNT(*) FROM capabilities WHERE kind=?", (kind,)
                ).fetchone()[0])
        with self._conn() as con:
            return int(con.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0])
