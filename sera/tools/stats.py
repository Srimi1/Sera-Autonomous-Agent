"""Per-tool quality stats — usage / success / latency / $/call.

Outclass: `sera tools --stats` shows real numbers per tool. Drift becomes visible:
a tool that worked yesterday but fails today shows up immediately.

DB: ~/.sera/tool_stats.db. Recorded on every dispatcher.execute() call.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from sera.config import SERA_HOME

TOOL_STATS_DB = SERA_HOME / "tool_stats.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    error_msg TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tc_name ON tool_calls(tool_name, recorded_at);
"""


# ---------------------------------------------------------------------------
# Result row
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolStatRow:
    tool_name: str
    n_calls: int
    n_ok: int
    success_pct: float
    p50_ms: int
    avg_latency_ms: float
    avg_cost_usd: float
    last_used_at: float

    @property
    def n_fail(self) -> int:
        return self.n_calls - self.n_ok


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def _conn(path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    db = path or TOOL_STATS_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

def record_tool_call(
    *,
    tool_name: str,
    latency_ms: int,
    success: bool,
    error_msg: str | None = None,
    cost_usd: float = 0.0,
    _db: Path | None = None,
) -> None:
    """Record one tool execution. Called from dispatcher.execute after each call."""
    with _conn(_db) as con:
        con.execute(
            "INSERT INTO tool_calls "
            "(tool_name, latency_ms, success, error_msg, cost_usd, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                tool_name,
                int(latency_ms),
                int(success),
                error_msg,
                float(cost_usd),
                time.time(),
            ),
        )
        con.commit()


def total_calls(_db: Path | None = None) -> int:
    db = _db or TOOL_STATS_DB
    if not db.exists():
        return 0
    with _conn(db) as con:
        return int(con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0])


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def tool_stats(_db: Path | None = None) -> list[ToolStatRow]:
    """Per-tool aggregate: n_calls, success rate, p50, avg latency, avg cost."""
    db = _db or TOOL_STATS_DB
    if not db.exists():
        return []
    with _conn(db) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT tool_name, latency_ms, success, cost_usd, recorded_at "
            "FROM tool_calls ORDER BY tool_name, latency_ms"
        ).fetchall()
    if not rows:
        return []

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["tool_name"], []).append(r)

    out: list[ToolStatRow] = []
    for name, calls in sorted(groups.items()):
        n = len(calls)
        n_ok = sum(1 for c in calls if c["success"])
        latencies = sorted(c["latency_ms"] for c in calls)
        costs = [c["cost_usd"] for c in calls]
        last = max(c["recorded_at"] for c in calls)
        p50 = latencies[n // 2]
        avg_lat = sum(latencies) / n
        avg_cost = sum(costs) / n
        out.append(ToolStatRow(
            tool_name=name,
            n_calls=n,
            n_ok=n_ok,
            success_pct=n_ok / n * 100,
            p50_ms=p50,
            avg_latency_ms=avg_lat,
            avg_cost_usd=avg_cost,
            last_used_at=last,
        ))
    return out


def stats_for(tool_name: str, _db: Path | None = None) -> ToolStatRow | None:
    for row in tool_stats(_db):
        if row.tool_name == tool_name:
            return row
    return None


# ---------------------------------------------------------------------------
# Reset (for test isolation / dashboard rebuild)
# ---------------------------------------------------------------------------

def clear_stats(_db: Path | None = None) -> int:
    db = _db or TOOL_STATS_DB
    if not db.exists():
        return 0
    with _conn(db) as con:
        cur = con.execute("DELETE FROM tool_calls")
        con.commit()
        return int(cur.rowcount)
