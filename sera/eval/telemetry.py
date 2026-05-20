"""Eval telemetry — SQLite store of run / case outcomes.

Schema:

  runs    (id, started_at, finished_at, profile, n_pass, n_fail)
  results (id, run_id, case_id, latency_ms, tool_calls_count,
           input_tokens, output_tokens, cache_read_tokens,
           cache_creation_tokens, passed, reason)

The store is append-only — `sera eval show` reads from the last N runs.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sera.config import SERA_HOME, ensure_home

TELEMETRY_DB = SERA_HOME / "telemetry.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    profile TEXT,
    n_pass INTEGER NOT NULL DEFAULT 0,
    n_fail INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    case_id TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    tool_calls_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
"""


@dataclass
class TurnRow:
    case_id: str
    latency_ms: int
    tool_calls_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    passed: bool
    reason: str = ""


class TelemetryStore:
    """Light wrapper over the telemetry DB. Idempotent schema init."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or TELEMETRY_DB

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        ensure_home()
        # Make sure the parent of a non-default path exists too.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
        finally:
            conn.close()

    def start_run(self, profile: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, started_at, profile, n_pass, n_fail) "
                "VALUES (?, ?, ?, 0, 0)",
                (run_id, time.time(), profile),
            )
            c.commit()
        return run_id

    def record(self, run_id: str, row: TurnRow) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO results (run_id, case_id, latency_ms, tool_calls_count, "
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                "passed, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    row.case_id,
                    int(row.latency_ms),
                    int(row.tool_calls_count),
                    int(row.input_tokens),
                    int(row.output_tokens),
                    int(row.cache_read_tokens),
                    int(row.cache_creation_tokens),
                    1 if row.passed else 0,
                    row.reason or None,
                ),
            )
            c.execute(
                "UPDATE runs SET n_pass = n_pass + ?, n_fail = n_fail + ? WHERE id = ?",
                (1 if row.passed else 0, 0 if row.passed else 1, run_id),
            )
            c.commit()

    def finish_run(self, run_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at = ? WHERE id = ?",
                (time.time(), run_id),
            )
            c.commit()

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT id, started_at, finished_at, profile, n_pass, n_fail "
                "FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def results_for(self, run_id: str) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT case_id, latency_ms, tool_calls_count, input_tokens, "
                "output_tokens, cache_read_tokens, cache_creation_tokens, "
                "passed, reason FROM results WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
