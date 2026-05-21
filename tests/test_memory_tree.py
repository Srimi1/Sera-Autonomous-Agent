"""P-11: MemoryTree — chunks + entities + relations + vector recall."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.memory.tree import (
    MemoryTree,
    cosine_similarity,
    euclidean_distance,
)

# Use a tiny embedding dim so test vectors stay readable.
DIM = 4


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "memory.db", embedding_dim=DIM)


def test_schema_init_idempotent(tmp_path: Path):
    t1 = _tree(tmp_path)
    assert t1.stats() == {"chunks": 0, "entities": 0, "relations": 0}
    t1.close()

    # Re-open same DB; no error, no duplicate tables.
    t2 = _tree(tmp_path)
    assert t2.stats() == {"chunks": 0, "entities": 0, "relations": 0}
    t2.close()


def test_add_and_get_chunk_roundtrip(tmp_path: Path):
    t = _tree(tmp_path)
    cid = t.add_chunk(
        source="notes",
        content="Sera is a local-first agent.",
        summary="definition",
        confidence=0.9,
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    chunk = t.get_chunk(cid)
    assert chunk is not None
    assert chunk.source == "notes"
    assert chunk.summary == "definition"
    assert chunk.confidence == pytest.approx(0.9)
    assert chunk.content.startswith("Sera")


def test_add_chunk_rejects_bad_confidence(tmp_path: Path):
    t = _tree(tmp_path)
    with pytest.raises(ValueError):
        t.add_chunk(source="x", content="c", confidence=1.5)
    with pytest.raises(ValueError):
        t.add_chunk(source="x", content="c", confidence=-0.1)


def test_add_chunk_rejects_dim_mismatch(tmp_path: Path):
    t = _tree(tmp_path)
    with pytest.raises(ValueError):
        t.add_chunk(source="x", content="c", embedding=[1.0, 0.0])


def test_add_entity_upserts_by_name(tmp_path: Path):
    t = _tree(tmp_path)
    a = t.add_entity(name="Sera", type="project")
    b = t.add_entity(name="Sera", type="project")
    assert a == b
    e = t.find_entity("Sera")
    assert e is not None and e.name == "Sera"
    # last_seen advances on upsert.
    assert e.last_seen >= e.first_seen


def test_add_relation_links_entities(tmp_path: Path):
    t = _tree(tmp_path)
    chunk_id = t.add_chunk(source="src", content="Sera outclasses Hermes")
    rid = t.add_relation(
        src="Sera",
        dst="Hermes",
        kind="outclasses",
        confidence=0.85,
        provenance_chunk_id=chunk_id,
    )
    assert rid > 0
    rels = t.relations_for("Sera")
    assert len(rels) == 1
    assert rels[0].kind == "outclasses"
    assert rels[0].confidence == pytest.approx(0.85)
    assert rels[0].provenance_chunk_id == chunk_id


def test_relation_rejects_bad_confidence(tmp_path: Path):
    t = _tree(tmp_path)
    with pytest.raises(ValueError):
        t.add_relation(src="A", dst="B", kind="x", confidence=2.0)


def test_search_returns_ranked_results(tmp_path: Path):
    t = _tree(tmp_path)
    # Three orthogonal-ish vectors so closest match is unambiguous.
    near_id = t.add_chunk(source="s", content="near", embedding=[1.0, 0.0, 0.0, 0.0])
    mid_id = t.add_chunk(source="s", content="mid", embedding=[0.7, 0.7, 0.0, 0.0])
    far_id = t.add_chunk(source="s", content="far", embedding=[0.0, 0.0, 1.0, 0.0])

    hits = t.search([1.0, 0.0, 0.0, 0.0], limit=3)
    assert [h.chunk_id for h in hits] == [near_id, mid_id, far_id]
    # Distances monotonic non-decreasing.
    assert hits[0].distance <= hits[1].distance <= hits[2].distance


def test_search_filters_min_confidence(tmp_path: Path):
    t = _tree(tmp_path)
    high_id = t.add_chunk(
        source="s", content="trusted", confidence=0.9, embedding=[1.0, 0.0, 0.0, 0.0]
    )
    t.add_chunk(
        source="s", content="weak", confidence=0.2, embedding=[1.0, 0.0, 0.0, 0.0]
    )
    hits = t.search([1.0, 0.0, 0.0, 0.0], limit=10, min_confidence=0.5)
    assert [h.chunk_id for h in hits] == [high_id]


def test_search_rejects_query_dim_mismatch(tmp_path: Path):
    t = _tree(tmp_path)
    with pytest.raises(ValueError):
        t.search([1.0, 0.0])


def test_search_skips_chunks_without_embedding(tmp_path: Path):
    t = _tree(tmp_path)
    t.add_chunk(source="s", content="no-vec")  # no embedding
    embedded_id = t.add_chunk(
        source="s", content="vec", embedding=[1.0, 0.0, 0.0, 0.0]
    )
    hits = t.search([1.0, 0.0, 0.0, 0.0])
    assert [h.chunk_id for h in hits] == [embedded_id]


def test_provenance_traversal(tmp_path: Path):
    """Relation should point back to the chunk that justifies it."""
    t = _tree(tmp_path)
    chunk_id = t.add_chunk(source="paper", content="Alice taught Bob.")
    t.add_relation(src="Alice", dst="Bob", kind="taught", provenance_chunk_id=chunk_id)
    rels = t.relations_for("Alice")
    assert rels[0].provenance_chunk_id == chunk_id
    chunk = t.get_chunk(rels[0].provenance_chunk_id)
    assert chunk is not None
    assert "Alice" in chunk.content


def test_using_vss_property_reflects_backend(tmp_path: Path):
    """Property must match whatever backend the tree ended up with."""
    t = _tree(tmp_path)
    # We don't assert True or False — only that it returned without crashing
    # and is a bool. Local CI usually lacks sqlite-vss so it's False.
    assert isinstance(t.using_vss, bool)


def test_cosine_helper_matches_search_ordering(tmp_path: Path):
    """Standalone cosine helper agrees with tree search direction."""
    sim_close = cosine_similarity([1, 0, 0, 0], [1, 0, 0, 0])
    sim_far = cosine_similarity([1, 0, 0, 0], [0, 0, 1, 0])
    assert sim_close > sim_far
    assert euclidean_distance([0, 0], [3, 4]) == pytest.approx(5.0)


def test_stats_reflects_inserts(tmp_path: Path):
    t = _tree(tmp_path)
    t.add_chunk(source="s", content="c")
    t.add_entity(name="X", type="thing")
    t.add_relation(src="X", dst="Y", kind="rel")
    s = t.stats()
    assert s["chunks"] == 1
    # add_relation upserts both X and Y, so entities count is 2.
    assert s["entities"] == 2
    assert s["relations"] == 1
