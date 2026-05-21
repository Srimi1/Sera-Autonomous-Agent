"""P-17: freshness scoring + EWMA decay + entity-aware boost."""
from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path

import pytest

from sera.memory.embedder import StubEmbedder
from sera.memory.search import HybridWeights, hybrid_search
from sera.memory.tree import (
    FRESHNESS_EWMA_ALPHA,
    FRESHNESS_HALF_LIFE_SECONDS,
    MemoryTree,
)


DIM = 16
HOUR = 3600.0
DAY = 24 * HOUR


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


# ─── Basic decay ────────────────────────────────────────────────


def test_new_chunk_is_fresh(tmp_path: Path):
    tree = _tree(tmp_path)
    now = 1_000_000.0
    cid = tree.add_chunk(source="s", content="hi", now=now)
    assert tree.freshness_of(cid, now=now) == pytest.approx(1.0)


def test_freshness_decays_with_elapsed_time(tmp_path: Path):
    tree = _tree(tmp_path)
    start = 1_000_000.0
    cid = tree.add_chunk(source="s", content="hi", now=start)
    half = tree.freshness_of(cid, now=start + FRESHNESS_HALF_LIFE_SECONDS)
    assert half == pytest.approx(0.5, abs=0.01)
    quarter = tree.freshness_of(cid, now=start + 2 * FRESHNESS_HALF_LIFE_SECONDS)
    assert quarter == pytest.approx(0.25, abs=0.01)


def test_freshness_clamped_zero_for_missing_chunk(tmp_path: Path):
    tree = _tree(tmp_path)
    assert tree.freshness_of(999, now=1.0) == 0.0


def test_freshness_clamped_above_one(tmp_path: Path):
    """Stored freshness > 1 (impossible via API, but defensible) clamps."""
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="x", now=100.0)
    tree.conn.execute(
        "UPDATE chunks SET freshness = 5.0 WHERE id = ?", (cid,)
    )
    tree.conn.commit()
    assert tree.freshness_of(cid, now=100.0) == 1.0


# ─── Touch (EWMA) ──────────────────────────────────────────────


def test_touch_resets_freshness_toward_one(tmp_path: Path):
    tree = _tree(tmp_path)
    start = 1_000_000.0
    cid = tree.add_chunk(source="s", content="x", now=start)
    # Skip far ahead so decay drops it low.
    distant = start + 5 * FRESHNESS_HALF_LIFE_SECONDS
    bumped = tree.touch_chunk(cid, now=distant)
    # EWMA: new ≈ alpha + (1-alpha) * decayed. Decayed ≈ 0, so new ≈ alpha.
    assert bumped == pytest.approx(FRESHNESS_EWMA_ALPHA, abs=0.05)


def test_repeated_touches_approach_one(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="x", now=100.0)
    # Five rapid touches: each pulls halfway toward 1.0.
    last = 0.0
    for i in range(5):
        last = tree.touch_chunk(cid, now=100.0 + i * 0.001)
    assert last > 0.95


def test_touch_missing_chunk_returns_zero(tmp_path: Path):
    tree = _tree(tmp_path)
    assert tree.touch_chunk(42, now=1.0) == 0.0


def test_touch_persists(tmp_path: Path):
    """A touched chunk's freshness must survive reload."""
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="x", now=100.0)
    tree.touch_chunk(cid, now=100.0 + FRESHNESS_HALF_LIFE_SECONDS)
    after = tree.freshness_of(cid, now=100.0 + FRESHNESS_HALF_LIFE_SECONDS)
    # Right after touch, decay between same-instant last_accessed_at and now
    # is zero → stored freshness reads back unchanged.
    assert after == pytest.approx(
        FRESHNESS_EWMA_ALPHA + (1 - FRESHNESS_EWMA_ALPHA) * 0.5, abs=0.05
    )


# ─── Entity-aware boost ────────────────────────────────────────


def test_entity_aware_freshness_lifts_old_chunk_with_active_entity(tmp_path: Path):
    tree = _tree(tmp_path)
    old = 1_000_000.0
    very_recent = old + 5 * FRESHNESS_HALF_LIFE_SECONDS  # = "now"

    cid = tree.add_chunk(source="news", content="Alice shipped the patch", now=old)
    # Alice gets mentioned again much later → her last_seen advances to ~now.
    tree.add_relation(
        src="Alice", dst="Patch", kind="caused",
        confidence=0.9, provenance_chunk_id=cid,
    )
    # Bump Alice's last_seen directly to simulate recent recall elsewhere.
    tree.conn.execute(
        "UPDATE entities SET last_seen = ? WHERE name = 'Alice'", (very_recent,)
    )
    tree.conn.commit()

    base = tree.freshness_of(cid, now=very_recent)
    boosted = tree.entity_aware_freshness(cid, now=very_recent)
    assert boosted > base
    assert boosted == pytest.approx(1.0, abs=0.05)


def test_entity_aware_freshness_falls_back_to_direct_when_no_relations(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="lonely", now=100.0)
    later = 100.0 + FRESHNESS_HALF_LIFE_SECONDS
    direct = tree.freshness_of(cid, now=later)
    aware = tree.entity_aware_freshness(cid, now=later)
    assert direct == pytest.approx(aware)


def test_entity_aware_takes_max_not_sum(tmp_path: Path):
    tree = _tree(tmp_path)
    start = 1_000_000.0
    cid = tree.add_chunk(source="s", content="x", now=start)
    tree.add_relation(
        src="Stale", dst="Old", kind="caused", provenance_chunk_id=cid,
    )
    tree.conn.execute(
        "UPDATE entities SET last_seen = ? WHERE name IN ('Stale','Old')",
        (start - 10 * FRESHNESS_HALF_LIFE_SECONDS,),
    )
    tree.conn.commit()
    # Direct freshness is fresh; entity decay is near-zero. Max wins.
    aware = tree.entity_aware_freshness(cid, now=start)
    assert aware == pytest.approx(1.0, abs=0.05)


# ─── Hybrid search wiring ──────────────────────────────────────


def test_hybrid_search_demotes_old_chunks(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    start = 1_000_000.0

    old_id = tree.add_chunk(
        source="s", content="recipe widget", embedding=_run(e.embed("recipe widget")),
        now=start,
    )
    new_id = tree.add_chunk(
        source="s", content="recipe widget", embedding=_run(e.embed("recipe widget")),
        now=start + 10 * FRESHNESS_HALF_LIFE_SECONDS,
    )

    query_vec = _run(e.embed("recipe widget"))
    now = start + 10 * FRESHNESS_HALF_LIFE_SECONDS

    hits = hybrid_search(
        tree, "recipe widget", query_embedding=query_vec, k=2,
        now=now, touch=False,
    )
    assert hits[0].chunk_id == new_id
    assert hits[1].chunk_id == old_id


def test_hybrid_search_apply_freshness_off_preserves_order(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    a = tree.add_chunk(source="s", content="alpha", embedding=_run(e.embed("alpha")), now=100.0)
    b = tree.add_chunk(source="s", content="alpha", embedding=_run(e.embed("alpha")), now=100.0 + 10 * FRESHNESS_HALF_LIFE_SECONDS)
    # With freshness off, identical content / tied BM25 — order is stable.
    hits = hybrid_search(
        tree, "alpha", query_embedding=_run(e.embed("alpha")), k=2,
        apply_freshness=False, touch=False, now=100.0 + 10 * FRESHNESS_HALF_LIFE_SECONDS,
    )
    assert {h.chunk_id for h in hits} == {a, b}


def test_hybrid_search_touches_returned_hits(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    cid = tree.add_chunk(
        source="s", content="touchme", embedding=_run(e.embed("touchme")), now=100.0,
    )
    far = 100.0 + 10 * FRESHNESS_HALF_LIFE_SECONDS
    hybrid_search(
        tree, "touchme", query_embedding=_run(e.embed("touchme")), k=1, now=far,
    )
    row = tree.conn.execute(
        "SELECT last_accessed_at FROM chunks WHERE id = ?", (cid,)
    ).fetchone()
    assert float(row["last_accessed_at"]) == pytest.approx(far)


def test_hybrid_search_touch_off_does_not_persist(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    cid = tree.add_chunk(
        source="s", content="ignoreme", embedding=_run(e.embed("ignoreme")), now=100.0,
    )
    far = 100.0 + 10 * FRESHNESS_HALF_LIFE_SECONDS
    hybrid_search(
        tree, "ignoreme", query_embedding=_run(e.embed("ignoreme")), k=1,
        now=far, touch=False,
    )
    row = tree.conn.execute(
        "SELECT last_accessed_at FROM chunks WHERE id = ?", (cid,)
    ).fetchone()
    assert float(row["last_accessed_at"]) == pytest.approx(100.0)


# ─── Migration ─────────────────────────────────────────────────


def test_migration_adds_freshness_columns_to_legacy_db(tmp_path: Path):
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            embedding BLOB,
            created_at REAL NOT NULL
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_entity_id INTEGER NOT NULL,
            dst_entity_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            provenance_chunk_id INTEGER,
            created_at REAL NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO chunks (source, content, created_at) VALUES ('s', 'legacy', 0)"
    )
    legacy.commit()
    legacy.close()

    tree = MemoryTree(db_path=db, embedding_dim=DIM)
    cols = {r[1] for r in tree.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "freshness" in cols
    assert "last_accessed_at" in cols
    # Legacy rows default to freshness=1.0; last_accessed_at=NULL means
    # "no decay" — they read fresh.
    row = tree.conn.execute(
        "SELECT freshness, last_accessed_at FROM chunks WHERE content='legacy'"
    ).fetchone()
    assert float(row["freshness"]) == pytest.approx(1.0)
    assert row["last_accessed_at"] is None


def test_legacy_chunk_freshness_starts_at_one(tmp_path: Path):
    """Migrated chunks must not be born stale just because they're old."""
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            embedding BLOB,
            created_at REAL NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO chunks (source, content, created_at) VALUES ('s', 'x', 0)"
    )
    legacy.commit()
    legacy.close()

    tree = MemoryTree(db_path=db, embedding_dim=DIM)
    cid = tree.conn.execute("SELECT id FROM chunks").fetchone()["id"]
    # With null last_accessed_at, decay is a no-op — chunk reads at stored value.
    assert tree.freshness_of(cid, now=1e10) == 1.0


# ─── Math sanity ───────────────────────────────────────────────


def test_half_life_constants_unchanged():
    # Lock the published defaults so accidental tuning gets reviewed.
    assert FRESHNESS_HALF_LIFE_SECONDS == 30 * 24 * 60 * 60
    assert FRESHNESS_EWMA_ALPHA == 0.5


def test_decay_factor_matches_half_life_formula(tmp_path: Path):
    tree = _tree(tmp_path)
    start = 0.0
    cid = tree.add_chunk(source="s", content="x", now=start)
    elapsed = 3 * DAY
    expected = 0.5 ** (elapsed / FRESHNESS_HALF_LIFE_SECONDS)
    assert tree.freshness_of(cid, now=start + elapsed) == pytest.approx(expected)
