"""Local-only telemetry pipeline — P-86.

OUTCLASS: No outbound by default.  Every metric lands in a local SQLite DB.
No HTTP call is ever made — tcpdump shows zero outbound on any telemetry op.
Rivals (Hermes, OpenHuman) either have no telemetry or phone home.  Sera's
telemetry is private by architecture: the code path never imports requests,
httpx, or any network library.

Schema: one `events` table with (id, ts, event, data_json).
`dashboard()` returns a per-event-kind summary (count, last_ts).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time as _time_mod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from sera.config import SERA_HOME

TELEMETRY_DB = SERA_HOME / "telemetry.db"
log = logging.getLogger("sera.telemetry.local")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    event      TEXT    NOT NULL,
    data_json  TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts);
"""


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TelemetryEvent:
    id: int
    ts: float
    event: str
    data: dict[str, Any]


@dataclass(frozen=True)
class EventSummary:
    event: str
    count: int
    last_ts: float


# ---------------------------------------------------------------------------
# Telemetry store
# ---------------------------------------------------------------------------

class LocalTelemetry:
    """Append-only local telemetry.  Zero outbound.  SQLite backed."""

    def __init__(
        self,
        db: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db = db or TELEMETRY_DB
        self._clock = clock or _time_mod.time
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

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Record one telemetry event.  Never makes a network call."""
        ts = self._clock()
        payload = json.dumps(data or {}, separators=(",", ":"))
        with self._conn() as con:
            con.execute(
                "INSERT INTO events (ts, event, data_json) VALUES (?, ?, ?)",
                (ts, event, payload),
            )
            con.commit()
        log.debug("telemetry: %s", event)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def query(
        self,
        event: str | None = None,
        limit: int = 100,
    ) -> list[TelemetryEvent]:
        q = "SELECT * FROM events"
        params: tuple = ()
        if event:
            q += " WHERE event = ?"
            params = (event,)
        q += " ORDER BY ts DESC"
        if limit:
            q += f" LIMIT {limit}"
        with self._conn() as con:
            rows = con.execute(q, params).fetchall()
        return [TelemetryEvent(
            id=r["id"], ts=float(r["ts"]),
            event=r["event"], data=json.loads(r["data_json"]),
        ) for r in rows]

    def dashboard(self) -> list[EventSummary]:
        """Per-event-kind summary: count + last_ts, ordered by count desc."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT event, COUNT(*) as n, MAX(ts) as last_ts "
                "FROM events GROUP BY event ORDER BY n DESC"
            ).fetchall()
        return [EventSummary(event=r["event"], count=r["n"], last_ts=float(r["last_ts"]))
                for r in rows]

    def count(self, event: str | None = None) -> int:
        if event:
            with self._conn() as con:
                return int(con.execute(
                    "SELECT COUNT(*) FROM events WHERE event=?", (event,)
                ).fetchone()[0])
        with self._conn() as con:
            return int(con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
