"""Response distillation cache: result-level cache by (prompt-hash, tool-trace-hash).

Repeated queries cost cents, not dollars. A cache hit skips the LLM call entirely
and returns the stored response, tracking cost saved per entry.

Key = SHA-256( user_message + "\x00" + tool_trace_fingerprint )

Heritage: no rival ships response-level distillation cache — this is the outclass.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from sera.config import SERA_HOME

DISTILL_CACHE_DB = SERA_HOME / "distill_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS distill_cache (
    key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL
);
CREATE INDEX IF NOT EXISTS idx_dc_created ON distill_cache(created_at);
"""

DEFAULT_TTL_S: int = 86_400        # 24 hours
DEFAULT_MAX_ENTRIES: int = 1_000


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def compute_key(user_msg: str, tool_msgs: list[dict[str, Any]]) -> str:
    """Derive a stable cache key from the user message and tool result trace.

    Only the tool result content is hashed (not IDs or timestamps) so that
    semantically identical traces produce the same key regardless of session.
    """
    trace = [
        {"name": m.get("name", ""), "content": m.get("content", "")}
        for m in tool_msgs
        if m.get("role") == "tool"
    ]
    payload = json.dumps({"msg": user_msg, "trace": trace}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache store
# ---------------------------------------------------------------------------

@dataclass
class CacheStats:
    entries: int
    total_hits: int
    cost_saved_usd: float
    hit_rate: float      # hits / (entries + hits); 0 if no data


class DistillCache:
    """SQLite-backed response distillation cache.

    Thread-safe for single-process use (SQLite WAL mode).
    Each entry stores the response, its original cost, hit count, and timestamps.
    """

    def __init__(self, db: Path | None = None, ttl_s: int = DEFAULT_TTL_S) -> None:
        self._db = db or DISTILL_CACHE_DB
        self._ttl_s = ttl_s
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)
            con.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Core get / put
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return cached response or None on miss. Increments hit counter."""
        now = time.time()
        cutoff = now - self._ttl_s
        with self._conn() as con:
            row = con.execute(
                "SELECT response, created_at FROM distill_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if row["created_at"] < cutoff:
                con.execute("DELETE FROM distill_cache WHERE key = ?", (key,))
                con.commit()
                return None
            con.execute(
                "UPDATE distill_cache SET hits = hits + 1, last_hit_at = ? WHERE key = ?",
                (now, key),
            )
            con.commit()
            return row["response"]

    def put(self, key: str, response: str, *, cost_usd: float = 0.0) -> None:
        """Store a response. Silently overwrites existing entry."""
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO distill_cache "
                "(key, response, cost_usd, created_at, hits, last_hit_at) "
                "VALUES (?, ?, ?, ?, 0, NULL)",
                (key, response, cost_usd, time.time()),
            )
            con.commit()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def evict(
        self,
        *,
        max_age_s: int | None = None,
        max_entries: int | None = None,
    ) -> int:
        """Remove stale or excess entries. Returns number of rows deleted."""
        deleted = 0
        cutoff_age = max_age_s or self._ttl_s
        with self._conn() as con:
            cutoff_ts = time.time() - cutoff_age
            cur = con.execute(
                "DELETE FROM distill_cache WHERE created_at < ?", (cutoff_ts,)
            )
            deleted += cur.rowcount

            if max_entries is not None:
                cur2 = con.execute(
                    "SELECT COUNT(*) FROM distill_cache"
                ).fetchone()[0]
                if cur2 > max_entries:
                    excess = cur2 - max_entries
                    cur3 = con.execute(
                        "DELETE FROM distill_cache WHERE key IN "
                        "(SELECT key FROM distill_cache ORDER BY last_hit_at ASC NULLS FIRST LIMIT ?)",
                        (excess,),
                    )
                    deleted += cur3.rowcount

            con.commit()
        return deleted

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> CacheStats:
        """Return aggregate cache performance statistics."""
        with self._conn() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(hits), 0) AS total_hits, "
                "COALESCE(SUM(cost_usd * hits), 0.0) AS cost_saved "
                "FROM distill_cache"
            ).fetchone()
        n = int(row["n"])
        hits = int(row["total_hits"])
        cost_saved = float(row["cost_saved"])
        denom = n + hits
        hit_rate = hits / denom if denom else 0.0
        return CacheStats(
            entries=n,
            total_hits=hits,
            cost_saved_usd=cost_saved,
            hit_rate=hit_rate,
        )
