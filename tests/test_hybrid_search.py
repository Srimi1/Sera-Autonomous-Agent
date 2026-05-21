"""P-16: BM25 + vector + graph hybrid retrieval with RRF fusion."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from sera.memory.embedder import StubEmbedder
from sera.memory.search import (
    DEFAULT_RRF_K,
    HybridWeights,
    bm25_rank,
    graph_neighbours,
    hybrid_search,
    vector_rank,
)
from sera.memory.tree import MemoryTree


DIM = 16


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


# ─── Individual signals ──────────────────────────────────────────


def test_bm25_finds_exact_match(tmp_path: Path):
    tree = _tree(tmp_path)
    a = tree.add_chunk(source="s", content="quantum entanglement bell inequality")
    tree.add_chunk(source="s", content="potato salad recipe with mustard")
    ids = bm25_rank(tree, "quantum bell", limit=10)
    assert a in ids
    assert ids[0] == a


def test_bm25_empty_query_returns_empty(tmp_path: Path):
    tree = _tree(tmp_path)
    tree.add_chunk(source="s", content="anything")
    assert bm25_rank(tree, "", limit=5) == []
    assert bm25_rank(tree, "   ", limit=5) == []


def test_bm25_survives_operator_chars(tmp_path: Path):
    tree = _tree(tmp_path)
    a = tree.add_chunk(source="s", content="hello world example")
    # Reserved FTS5 chars in query — must not crash.
    ids = bm25_rank(tree, 'hello "world" (example):*', limit=5)
    assert a in ids


def test_vector_rank_orders_by_distance(tmp_path: Path):
    tree = _tree(tmp_path)
    near = tree.add_chunk(source="s", content="n", embedding=[1.0] + [0.0] * (DIM - 1))
    far = tree.add_chunk(source="s", content="f", embedding=[0.0] * (DIM - 1) + [1.0])
    ids = vector_rank(tree, [1.0] + [0.0] * (DIM - 1), limit=5)
    assert ids[0] == near
    assert ids[1] == far


def test_graph_neighbours_walks_entities_in_query(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="news", content="Alice ships the feature.")
    tree.add_relation(
        src="Alice", dst="Feature", kind="caused",
        confidence=0.9, provenance_chunk_id=cid,
    )
    hits = graph_neighbours(tree, "what did Alice do?", limit=5)
    assert hits == [cid]


def test_graph_neighbours_case_insensitive(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="ACME shipped Q3.")
    tree.add_relation(
        src="ACME", dst="Q3", kind="caused", provenance_chunk_id=cid,
    )
    hits = graph_neighbours(tree, "tell me about acme", limit=5)
    assert hits == [cid]


def test_graph_neighbours_ignores_relations_without_provenance(tmp_path: Path):
    tree = _tree(tmp_path)
    tree.add_relation(src="Alice", dst="Bob", kind="caused")  # no provenance
    hits = graph_neighbours(tree, "alice", limit=5)
    assert hits == []


def test_graph_neighbours_empty_query(tmp_path: Path):
    tree = _tree(tmp_path)
    assert graph_neighbours(tree, "", limit=5) == []


# ─── Fusion ──────────────────────────────────────────────────────


def test_hybrid_search_fuses_three_signals(tmp_path: Path):
    """A chunk surfaced by BM25 + vector + graph beats single-source rivals."""
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)

    target_content = "Alice fixed the cache invalidation bug"
    target_vec = _run(e.embed(target_content))
    target_id = tree.add_chunk(source="s", content=target_content, embedding=target_vec)
    tree.add_relation(
        src="Alice", dst="bug", kind="caused",
        confidence=0.9, provenance_chunk_id=target_id,
    )

    # Decoy: BM25-only match, unrelated semantics.
    tree.add_chunk(source="s", content="cache busting via etag headers")
    # Decoy: semantically close but no Alice entity.
    similar = "we patched the caching layer for invalidation"
    tree.add_chunk(source="s", content=similar, embedding=_run(e.embed(similar)))

    query = "what did Alice do about the cache invalidation"
    query_vec = _run(e.embed(query))
    hits = hybrid_search(tree, query, query_embedding=query_vec, k=5)

    assert hits[0].chunk_id == target_id
    assert {"bm25", "vector", "graph"} <= set(hits[0].sources)


def test_hybrid_score_increases_with_multi_signal_agreement(tmp_path: Path):
    """RRF math: multi-signal hits score higher than single-signal ones."""
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    multi = "Alice shipped the patch"
    vec_multi = _run(e.embed(multi))
    multi_id = tree.add_chunk(source="s", content=multi, embedding=vec_multi)
    tree.add_relation(
        src="Alice", dst="patch", kind="caused", provenance_chunk_id=multi_id,
    )

    bm_only = "the patch shipped yesterday"
    tree.add_chunk(source="s", content=bm_only)

    hits = hybrid_search(
        tree, "Alice patch", query_embedding=vec_multi, k=5
    )
    by_id = {h.chunk_id: h for h in hits}
    assert by_id[multi_id].score > max(
        h.score for cid, h in by_id.items() if cid != multi_id
    )


def test_hybrid_search_skips_vector_when_embedding_missing(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="standalone fact about Acme")
    hits = hybrid_search(tree, "Acme fact", k=5)
    assert any(h.chunk_id == cid for h in hits)
    # No vector embedding supplied → sources must not include 'vector'.
    for h in hits:
        assert "vector" not in h.sources


def test_hybrid_search_zero_weight_drops_signal(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    cid = tree.add_chunk(
        source="s", content="Alice did the thing", embedding=_run(e.embed("Alice did the thing"))
    )
    tree.add_relation(src="Alice", dst="thing", kind="caused", provenance_chunk_id=cid)

    weights = HybridWeights(bm25=1.0, vector=1.0, graph=0.0)
    hits = hybrid_search(
        tree, "Alice", query_embedding=_run(e.embed("Alice")),
        weights=weights, k=5,
    )
    for h in hits:
        assert "graph" not in h.sources


def test_hybrid_search_returns_at_most_k(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    for i in range(20):
        c = f"document {i} about widgets"
        tree.add_chunk(source="s", content=c, embedding=_run(e.embed(c)))
    hits = hybrid_search(
        tree, "widget document", query_embedding=_run(e.embed("widget")), k=5
    )
    assert len(hits) == 5


def test_hybrid_search_rejects_invalid_k(tmp_path: Path):
    tree = _tree(tmp_path)
    with pytest.raises(ValueError):
        hybrid_search(tree, "x", k=0)


def test_hybrid_beats_vector_only_when_query_term_missing(tmp_path: Path):
    """The phase's outclass spec: query terms that don't match the chunk
    body should still find it via BM25 + graph signals.
    """
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)

    # Target chunk uses domain-specific jargon; query uses everyday terms.
    target = "The CI pipeline regression Alice triaged last week is now closed"
    tvec = _run(e.embed(target))
    tid = tree.add_chunk(source="s", content=target, embedding=tvec)
    tree.add_relation(
        src="Alice", dst="regression", kind="caused", provenance_chunk_id=tid,
    )

    # Semantic decoys with no Alice link.
    for c in (
        "deployment scripts use feature flags",
        "code review checklist for backend changes",
        "weekly metrics dashboard rolled out",
    ):
        tree.add_chunk(source="s", content=c, embedding=_run(e.embed(c)))

    query = "the issue Alice mentioned last week"
    query_vec = _run(e.embed(query))

    vector_only = hybrid_search(
        tree, query, query_embedding=query_vec,
        weights=HybridWeights(bm25=0.0, vector=1.0, graph=0.0), k=3,
    )
    hybrid = hybrid_search(tree, query, query_embedding=query_vec, k=3)

    # Hybrid top-1 must be the target; vector-only may rank it lower or miss.
    assert hybrid[0].chunk_id == tid
    vector_rank_of_target = next(
        (i for i, h in enumerate(vector_only) if h.chunk_id == tid), None
    )
    hybrid_rank_of_target = 0
    if vector_rank_of_target is not None:
        assert hybrid_rank_of_target <= vector_rank_of_target


def test_default_rrf_k_unchanged():
    # Cormack et al. canonical k=60.
    assert DEFAULT_RRF_K == 60


# ─── FTS migration ───────────────────────────────────────────────


def test_fts_migration_backfills_legacy_db(tmp_path: Path):
    """A pre-P-16 DB whose chunks pre-date chunks_fts must be backfilled."""
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
        "INSERT INTO chunks (source, content, summary, created_at) "
        "VALUES ('s', 'legacy quantum widget', 'old', 0)"
    )
    legacy.commit()
    legacy.close()

    tree = MemoryTree(db_path=db, embedding_dim=DIM)
    ids = bm25_rank(tree, "quantum widget", limit=5)
    assert len(ids) == 1


def test_fts_index_tracks_update_chunk(tmp_path: Path):
    """update_chunk must keep chunks_fts in sync via the AU trigger."""
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="initial content")
    assert bm25_rank(tree, "initial", limit=5) == [cid]
    tree.update_chunk(cid, content="rewritten body")
    assert bm25_rank(tree, "initial", limit=5) == []
    assert bm25_rank(tree, "rewritten", limit=5) == [cid]


def test_fts_index_tracks_delete_chunk(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="ephemeral content")
    assert bm25_rank(tree, "ephemeral", limit=5) == [cid]
    tree.delete_chunk(cid)
    assert bm25_rank(tree, "ephemeral", limit=5) == []
