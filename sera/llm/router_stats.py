"""Per-turn LLM call stats: provider, model, task_kind, latency, cost, success rate.

Foundation for the bandit router (P-37+). Records every LLM call so the bandit
has a cold-start seed table to exploit immediately, not after 1000 warm-up turns.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from sera.config import SERA_HOME

ROUTER_STATS_DB = SERA_HOME / "router_stats.db"

# $/1M tokens — (input_rate, output_rate)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.00, 75.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS router_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    task_kind TEXT NOT NULL DEFAULT 'chat',
    latency_ms INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    success INTEGER NOT NULL DEFAULT 1,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rc_group
    ON router_calls(provider, model, task_kind);
"""


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _PRICING.get(model, (0.0, 0.0))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


@contextmanager
def _conn(path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    db = path or ROUTER_STATS_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    try:
        yield con
    finally:
        con.close()


def record_call(
    *,
    provider: str,
    model: str,
    task_kind: str,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    success: bool,
    _db: Path | None = None,
) -> None:
    """Record one LLM call. Called from the agent loop after every stream."""
    cost = _calc_cost(model, input_tokens, output_tokens)
    with _conn(_db) as con:
        con.execute(
            "INSERT INTO router_calls "
            "(provider, model, task_kind, latency_ms, input_tokens, "
            "output_tokens, cost_usd, success, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider,
                model,
                task_kind,
                int(latency_ms),
                int(input_tokens),
                int(output_tokens),
                cost,
                int(success),
                time.time(),
            ),
        )
        con.commit()


def cost_since(timestamp: float, *, task_kind: str | None = None, _db: Path | None = None) -> float:
    """Sum cost_usd for all calls recorded at or after `timestamp`.

    Optionally filter by task_kind. Returns 0.0 if DB missing or no rows.
    """
    db = _db or ROUTER_STATS_DB
    if not db.exists():
        return 0.0
    with _conn(db) as con:
        if task_kind is None:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM router_calls WHERE recorded_at >= ?",
                (timestamp,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM router_calls "
                "WHERE recorded_at >= ? AND task_kind = ?",
                (timestamp, task_kind),
            ).fetchone()
    return float(row[0])


def total_calls(_db: Path | None = None) -> int:
    db = _db or ROUTER_STATS_DB
    if not db.exists():
        return 0
    with _conn(db) as con:
        return con.execute("SELECT COUNT(*) FROM router_calls").fetchone()[0]


def p50_table(_db: Path | None = None) -> list[dict[str, Any]]:
    """Return per-(provider, model, task_kind) aggregated stats.

    Each row: provider, model, task_kind, n, p50_ms, avg_cost_usd, success_pct.
    """
    db = _db or ROUTER_STATS_DB
    if not db.exists():
        return []
    with _conn(db) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT provider, model, task_kind, latency_ms, cost_usd, success "
            "FROM router_calls "
            "ORDER BY provider, model, task_kind, latency_ms"
        ).fetchall()
    if not rows:
        return []

    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["provider"], r["model"], r["task_kind"])
        groups.setdefault(key, []).append(r)

    result: list[dict[str, Any]] = []
    for (provider, model, task_kind), calls in sorted(groups.items()):
        n = len(calls)
        latencies = sorted(r["latency_ms"] for r in calls)
        costs = [r["cost_usd"] for r in calls]
        n_ok = sum(1 for r in calls if r["success"])
        result.append({
            "provider": provider,
            "model": model,
            "task_kind": task_kind,
            "n": n,
            "p50_ms": latencies[n // 2],
            "avg_cost_usd": sum(costs) / n,
            "success_pct": n_ok / n * 100,
        })
    return result
