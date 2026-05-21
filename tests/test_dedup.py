"""P-18: provenance-preserving dedup + consolidation."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from sera.memory.embedder import StubEmbedder
from sera.memory.search import bm25_rank, hybrid_search
from sera.memory.tree import DEFAULT_DEDUP_THRESHOLD, MemoryTree


DIM = 16


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


# ─── Threshold + add_or_merge ──────────────────────────────────


def test_identical_content_merges(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("the cat is on the mat"))

    first, merged_first = tree.add_or_merge_chunk(
        source="src-a", content="the cat is on the mat", embedding=vec, now=100.0,
    )
    second, merged_second = tree.add_or_merge_chunk(
        source="src-b", content="the cat is on the mat", embedding=vec, now=110.0,
    )

    assert merged_first is False
    assert merged_second is True
    assert second == first
    # Only one canonical chunk lives in chunks.
    rows = tree.conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE merged_into IS NULL"
    ).fetchone()
    assert rows["n"] == 1


def test_provenance_accumulates_across_merges(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("Sera is local-first"))

    canonical, _ = tree.add_or_merge_chunk(
        source="src-a", content="Sera is local-first", embedding=vec, now=100.0,
    )
    tree.add_or_merge_chunk(
        source="src-b", content="Sera is local-first", embedding=vec, now=101.0,
    )
    tree.add_or_merge_chunk(
        source="src-c", content="Sera is local-first", embedding=vec, now=102.0,
    )

    trail = tree.merged_from_for(canonical)
    sources = [t["source"] for t in trail]
    assert sources == ["src-b", "src-c"]
    for entry in trail:
        assert "similarity" in entry and "at" in entry


def test_distinct_content_does_not_merge(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    a_id, _ = tree.add_or_merge_chunk(
        source="s", content="quantum entanglement bell",
        embedding=_run(e.embed("quantum entanglement bell")), now=100.0,
    )
    b_id, merged = tree.add_or_merge_chunk(
        source="s", content="potato salad mustard",
        embedding=_run(e.embed("potato salad mustard")), now=101.0,
    )
    assert merged is False
    assert a_id != b_id


def test_no_embedding_always_inserts(tmp_path: Path):
    """Without an embedding we can't compute similarity → always new row."""
    tree = _tree(tmp_path)
    a, am = tree.add_or_merge_chunk(source="s", content="x", embedding=None)
    b, bm = tree.add_or_merge_chunk(source="s", content="x", embedding=None)
    assert not am and not bm
    assert a != b


def test_merge_takes_max_confidence(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("fact about Acme"))
    cid, _ = tree.add_or_merge_chunk(
        source="s", content="fact about Acme",
        embedding=vec, confidence=0.4, now=100.0,
    )
    tree.add_or_merge_chunk(
        source="s", content="fact about Acme",
        embedding=vec, confidence=0.9, now=101.0,
    )
    row = tree.conn.execute(
        "SELECT confidence FROM chunks WHERE id = ?", (cid,)
    ).fetchone()
    assert float(row["confidence"]) == pytest.approx(0.9)


def test_merge_touches_freshness(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("dup target"))
    cid, _ = tree.add_or_merge_chunk(
        source="s", content="dup target", embedding=vec, now=100.0,
    )
    # Force the chunk to look stale.
    tree.conn.execute(
        "UPDATE chunks SET freshness = 0.1, last_accessed_at = ? WHERE id = ?",
        (100.0, cid),
    )
    tree.conn.commit()
    tree.add_or_merge_chunk(
        source="s2", content="dup target", embedding=vec, now=200.0,
    )
    f = tree.freshness_of(cid, now=200.0)
    assert f > 0.1


def test_find_near_duplicate_threshold(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec_a = _run(e.embed("alpha beta gamma"))
    cid = tree.add_chunk(source="s", content="alpha beta gamma", embedding=vec_a)

    # Identical query → similarity 1.0 → match at any threshold.
    found = tree.find_near_duplicate(vec_a, threshold=DEFAULT_DEDUP_THRESHOLD)
    assert found is not None
    assert found[0] == cid

    # Disjoint vocabulary → similarity 0 → no match.
    vec_b = _run(e.embed("unrelated lemma theorem"))
    assert tree.find_near_duplicate(vec_b, threshold=0.95) is None


def test_find_near_duplicate_threshold_bounds(tmp_path: Path):
    """A 0.0 threshold returns *any* hit, an unreachable threshold returns None."""
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    cid = tree.add_chunk(
        source="s", content="alpha", embedding=_run(e.embed("alpha")),
    )
    any_hit = tree.find_near_duplicate(_run(e.embed("alpha")), threshold=0.0)
    assert any_hit is not None and any_hit[0] == cid


# ─── Canonical resolution ──────────────────────────────────────


def test_resolve_canonical_follows_pointer(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("same content"))
    canonical, _ = tree.add_or_merge_chunk(
        source="a", content="same content", embedding=vec, now=100.0,
    )
    # Simulate a stale id pointing at the canonical (no merge yet inserts a
    # ghost row, so manufacture one for the test).
    tree.conn.execute(
        "UPDATE chunks SET merged_into = ? WHERE id = (SELECT MAX(id) FROM chunks)",
        (canonical,),
    )
    tree.conn.commit()
    # Find the merged row and resolve it.
    ghost = tree.conn.execute(
        "SELECT id FROM chunks WHERE merged_into IS NOT NULL"
    ).fetchone()
    if ghost is not None:
        assert tree.resolve_canonical(int(ghost["id"])) == canonical
    assert tree.resolve_canonical(canonical) == canonical


def test_resolve_canonical_handles_unknown_id(tmp_path: Path):
    tree = _tree(tmp_path)
    # Unknown id resolves to itself (no row to follow).
    assert tree.resolve_canonical(99999) == 99999


def test_resolve_canonical_cycle_safe(tmp_path: Path):
    """Manually poison the chain: A → B → A. resolve must not loop."""
    tree = _tree(tmp_path)
    a = tree.add_chunk(source="s", content="A")
    b = tree.add_chunk(source="s", content="B")
    tree.conn.execute("UPDATE chunks SET merged_into = ? WHERE id = ?", (b, a))
    tree.conn.execute("UPDATE chunks SET merged_into = ? WHERE id = ?", (a, b))
    tree.conn.commit()
    resolved = tree.resolve_canonical(a)
    assert resolved in {a, b}


# ─── Search skips merged rows ──────────────────────────────────


def test_bm25_skips_merged_into_rows(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("quantum widget"))
    canonical, _ = tree.add_or_merge_chunk(
        source="a", content="quantum widget", embedding=vec, now=100.0,
    )
    # Force-insert a ghost merged-into row with identical BM25 hits.
    tree.conn.execute(
        "INSERT INTO chunks (source, content, summary, confidence, embedding, "
        "freshness, last_accessed_at, merged_into, created_at) "
        "VALUES ('a', 'quantum widget', '', 1.0, NULL, 1.0, ?, ?, ?)",
        (100.0, canonical, 100.0),
    )
    tree.conn.commit()
    ids = bm25_rank(tree, "quantum widget", limit=10)
    assert canonical in ids
    ghosts = tree.conn.execute(
        "SELECT id FROM chunks WHERE merged_into IS NOT NULL"
    ).fetchall()
    assert all(int(g["id"]) not in ids for g in ghosts)


def test_vector_search_skips_merged(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("same content shape"))
    canonical, _ = tree.add_or_merge_chunk(
        source="a", content="same content shape", embedding=vec, now=100.0,
    )
    tree.conn.execute(
        "INSERT INTO chunks (source, content, summary, confidence, embedding, "
        "freshness, last_accessed_at, merged_into, created_at) "
        "VALUES ('a', 'same content shape', '', 1.0, ?, 1.0, ?, ?, ?)",
        (tree.conn.execute("SELECT embedding FROM chunks WHERE id = ?", (canonical,)).fetchone()["embedding"],
         100.0, canonical, 100.0),
    )
    tree.conn.commit()
    hits = tree.search(vec, limit=10)
    assert all(h.chunk_id == canonical for h in hits)


def test_hybrid_search_returns_canonical_only(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("fluffy cat on windowsill"))
    canonical, _ = tree.add_or_merge_chunk(
        source="a", content="fluffy cat on windowsill", embedding=vec, now=100.0,
    )
    # Three more dedupes → three merged_from entries, still one chunk.
    for s in ("b", "c", "d"):
        tree.add_or_merge_chunk(
            source=s, content="fluffy cat on windowsill",
            embedding=vec, now=100.0 + ord(s),
        )
    hits = hybrid_search(
        tree, "fluffy cat", query_embedding=vec, k=5, now=200.0, touch=False,
    )
    assert [h.chunk_id for h in hits] == [canonical]
    trail = tree.merged_from_for(canonical)
    assert [t["source"] for t in trail] == ["b", "c", "d"]


# ─── Migration ─────────────────────────────────────────────────


def test_legacy_db_migration_adds_merge_columns(tmp_path: Path):
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
    legacy.commit()
    legacy.close()

    tree = MemoryTree(db_path=db, embedding_dim=DIM)
    cols = {r[1] for r in tree.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "merged_into" in cols
    assert "merged_from" in cols


# ─── JSON round-trip ───────────────────────────────────────────


def test_merged_from_json_round_trip(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    vec = _run(e.embed("same content"))
    cid, _ = tree.add_or_merge_chunk(
        source="a", content="same content", embedding=vec, now=100.0,
    )
    tree.add_or_merge_chunk(
        source="b/with*weird:chars", content="same content", embedding=vec, now=101.0,
    )
    raw = tree.conn.execute(
        "SELECT merged_from FROM chunks WHERE id = ?", (cid,)
    ).fetchone()["merged_from"]
    decoded = json.loads(raw)
    assert decoded[0]["source"] == "b/with*weird:chars"
