"""Tests for sera.llm.distill_cache — response distillation cache."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sera.llm.distill_cache import CacheStats, DistillCache, compute_key


# ---------------------------------------------------------------------------
# compute_key
# ---------------------------------------------------------------------------

class TestComputeKey:
    def test_same_msg_same_trace_same_key(self) -> None:
        k1 = compute_key("hello", [])
        k2 = compute_key("hello", [])
        assert k1 == k2

    def test_different_msg_different_key(self) -> None:
        k1 = compute_key("hello", [])
        k2 = compute_key("world", [])
        assert k1 != k2

    def test_different_trace_different_key(self) -> None:
        msgs_a = [{"role": "tool", "name": "read", "content": "foo"}]
        msgs_b = [{"role": "tool", "name": "read", "content": "bar"}]
        k1 = compute_key("query", msgs_a)
        k2 = compute_key("query", msgs_b)
        assert k1 != k2

    def test_non_tool_msgs_ignored(self) -> None:
        # assistant and user messages don't change the tool trace
        msgs_a = [{"role": "assistant", "content": "thinking..."},
                  {"role": "tool", "name": "f", "content": "result"}]
        msgs_b = [{"role": "tool", "name": "f", "content": "result"}]
        assert compute_key("q", msgs_a) == compute_key("q", msgs_b)

    def test_key_is_hex_string(self) -> None:
        k = compute_key("test", [])
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)


# ---------------------------------------------------------------------------
# DistillCache — basic get / put
# ---------------------------------------------------------------------------

@pytest.fixture()
def cache(tmp_path: Path) -> DistillCache:
    return DistillCache(db=tmp_path / "dc.db")


class TestGetPut:
    def test_miss_returns_none(self, cache: DistillCache) -> None:
        assert cache.get("nonexistent") is None

    def test_put_then_get(self, cache: DistillCache) -> None:
        cache.put("k1", "response text")
        assert cache.get("k1") == "response text"

    def test_overwrite(self, cache: DistillCache) -> None:
        cache.put("k1", "old")
        cache.put("k1", "new")
        assert cache.get("k1") == "new"

    def test_get_increments_hits(self, cache: DistillCache) -> None:
        cache.put("k1", "r")
        cache.get("k1")
        cache.get("k1")
        stats = cache.stats()
        assert stats.total_hits == 2

    def test_unicode_response(self, cache: DistillCache) -> None:
        cache.put("k", "日本語テスト 🤖")
        assert cache.get("k") == "日本語テスト 🤖"


# ---------------------------------------------------------------------------
# DistillCache — TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_expired_entry_returns_none(self, tmp_path: Path) -> None:
        cache = DistillCache(db=tmp_path / "ttl.db", ttl_s=1)
        cache.put("k", "response")
        # Manually backdate the created_at to simulate expiry
        import sqlite3
        con = sqlite3.connect(tmp_path / "ttl.db")
        con.execute("UPDATE distill_cache SET created_at = ? WHERE key = ?",
                    (time.time() - 10, "k"))
        con.commit()
        con.close()
        assert cache.get("k") is None  # expired, deleted

    def test_fresh_entry_not_expired(self, cache: DistillCache) -> None:
        cache.put("k", "response")
        assert cache.get("k") == "response"


# ---------------------------------------------------------------------------
# DistillCache — evict
# ---------------------------------------------------------------------------

class TestEvict:
    def test_evict_old_entries(self, tmp_path: Path) -> None:
        cache = DistillCache(db=tmp_path / "ev.db")
        cache.put("old", "r1")
        import sqlite3
        con = sqlite3.connect(tmp_path / "ev.db")
        con.execute("UPDATE distill_cache SET created_at = ? WHERE key = 'old'",
                    (time.time() - 90_000,))
        con.commit()
        con.close()
        cache.put("fresh", "r2")
        deleted = cache.evict(max_age_s=3600)
        assert deleted >= 1
        assert cache.get("old") is None
        assert cache.get("fresh") == "r2"

    def test_evict_excess_entries(self, tmp_path: Path) -> None:
        cache = DistillCache(db=tmp_path / "ex.db")
        for i in range(5):
            cache.put(f"key{i}", f"val{i}")
        deleted = cache.evict(max_entries=3)
        assert deleted == 2
        stats = cache.stats()
        assert stats.entries == 3


# ---------------------------------------------------------------------------
# DistillCache — stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_empty_stats(self, cache: DistillCache) -> None:
        s = cache.stats()
        assert s.entries == 0
        assert s.total_hits == 0
        assert s.hit_rate == 0.0
        assert s.cost_saved_usd == 0.0

    def test_cost_saved_tracks_hits(self, cache: DistillCache) -> None:
        cache.put("k", "response", cost_usd=0.01)
        cache.get("k")
        cache.get("k")
        s = cache.stats()
        assert s.cost_saved_usd == pytest.approx(0.02)  # 2 hits × $0.01

    def test_hit_rate_calculation(self, cache: DistillCache) -> None:
        cache.put("k", "r", cost_usd=0.01)
        cache.get("k")     # hit 1
        cache.get("k")     # hit 2
        cache.get("k")     # hit 3
        s = cache.stats()
        # entries=1, hits=3 → hit_rate = 3/(1+3) = 0.75
        assert s.hit_rate == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Bench verification (P-40 criterion)
# ---------------------------------------------------------------------------

class TestBenchVerification:
    """Cache hit rate > 60% and cost savings ≥ 50% on repeated workloads."""

    def test_repeated_workload_hit_rate(self, cache: DistillCache) -> None:
        key = compute_key("summarize this document", [])
        cost_per_call = 0.01

        # Simulate 10 identical queries: 1 miss, 9 hits
        hits = 0
        misses = 0
        for i in range(10):
            result = cache.get(key)
            if result is None:
                misses += 1
                cache.put(key, "The document summarizes X.", cost_usd=cost_per_call)
            else:
                hits += 1

        assert misses == 1
        assert hits == 9
        hit_rate = hits / (hits + misses)
        assert hit_rate > 0.60, f"hit_rate={hit_rate:.2f} < 0.60"

    def test_repeated_workload_cost_savings(self, cache: DistillCache) -> None:
        key = compute_key("plan my week", [])
        cost_per_call = 0.02

        # 1 miss + 9 hits
        cache.get(key)  # miss
        cache.put(key, "Here is your plan.", cost_usd=cost_per_call)
        for _ in range(9):
            cache.get(key)

        s = cache.stats()
        total_naive_cost = 10 * cost_per_call  # 0.20 if no cache
        saved_pct = s.cost_saved_usd / total_naive_cost
        assert saved_pct >= 0.50, f"cost savings={saved_pct:.1%} < 50%"

    def test_different_keys_independent(self, cache: DistillCache) -> None:
        k1 = compute_key("query A", [])
        k2 = compute_key("query B", [])
        cache.put(k1, "answer A", cost_usd=0.01)
        assert cache.get(k1) == "answer A"
        assert cache.get(k2) is None  # different key → miss
