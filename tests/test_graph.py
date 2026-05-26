"""P-15: entity extractor + typed causal-edge graph."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sera.memory.graph import (
    EDGE_KINDS,
    ExtractedEdge,
    ExtractedEntity,
    ExtractionResult,
    LLMExtractor,
    StubExtractor,
    UnknownEdgeKind,
    backfill,
    causal_chain,
    extract_and_persist,
    parse_llm_extraction,
)
from sera.memory.tree import MemoryTree


DIM = 8


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


# ─── EdgeKind contract ──────────────────────────────────────────


def test_edge_kinds_is_closed_vocabulary():
    assert set(EDGE_KINDS) == {
        "mentions",
        "works_at",
        "parent_of",
        "caused",
        "refuted_by",
        "supersedes",
        "similar_to",
    }


def test_extract_and_persist_rejects_unknown_kind(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="anything")

    class _Bad:
        async def extract(self, _text):
            return ExtractionResult(
                entities=(ExtractedEntity(name="A"), ExtractedEntity(name="B")),
                edges=(ExtractedEdge(src="A", dst="B", kind="frobnicates"),),
            )

    with pytest.raises(UnknownEdgeKind):
        _run(extract_and_persist(tree, cid, _Bad()))


# ─── Stub regex patterns ────────────────────────────────────────


@pytest.mark.parametrize(
    "text,kind,src,dst",
    [
        ("Alpha caused Beta", "caused", "Alpha", "Beta"),
        ("Carol works at Acme", "works_at", "Carol", "Acme"),
        ("Dragon is parent of Smaug", "parent_of", "Dragon", "Smaug"),
        ("Theory was refuted by Evidence", "refuted_by", "Theory", "Evidence"),
        ("Model supersedes Old Model", "supersedes", "Model", "Old Model"),
        ("Algo is similar to Algo Two", "similar_to", "Algo", "Algo Two"),
    ],
)
def test_stub_extractor_catches_each_pattern(text, kind, src, dst):
    res = _run(StubExtractor().extract(text))
    edges = [(e.src, e.dst, e.kind) for e in res.edges]
    assert (src, dst, kind) in edges


def test_stub_extractor_dedupes_entities():
    text = "Alice caused Bob. Alice caused Carol."
    res = _run(StubExtractor().extract(text))
    names = sorted(e.name for e in res.entities)
    assert names == ["Alice", "Bob", "Carol"]


def test_stub_extractor_empty_input():
    res = _run(StubExtractor().extract(""))
    assert not res
    assert res.entities == ()


# ─── Persistence ────────────────────────────────────────────────


def test_extract_and_persist_writes_entities_and_edges(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="news", content="Alpha caused Beta.")
    res = _run(extract_and_persist(tree, cid, StubExtractor()))
    assert any(e.name == "Alpha" for e in res.entities)
    rels = tree.relations_for("Alpha")
    assert len(rels) == 1
    assert rels[0].kind == "caused"
    assert rels[0].provenance_chunk_id == cid


def test_extract_and_persist_stamps_extracted_at(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="Alpha caused Beta.")
    assert tree.chunks_pending_extraction() == [cid]
    _run(extract_and_persist(tree, cid, StubExtractor()))
    assert tree.chunks_pending_extraction() == []


def test_extract_and_persist_missing_chunk(tmp_path: Path):
    tree = _tree(tmp_path)
    with pytest.raises(KeyError):
        _run(extract_and_persist(tree, 999, StubExtractor()))


# ─── Backfill ───────────────────────────────────────────────────


def test_backfill_processes_only_pending(tmp_path: Path):
    tree = _tree(tmp_path)
    tree.add_chunk(source="s", content="Alpha caused Beta.")
    tree.add_chunk(source="s", content="Carol works at Acme.")
    stats = _run(backfill(tree, StubExtractor()))
    assert stats.chunks_processed == 2
    assert stats.edges_written >= 2
    # Second pass is a no-op.
    stats2 = _run(backfill(tree, StubExtractor()))
    assert stats2.chunks_processed == 0
    # Unrelated chunks added later still pick up on next run.
    tree.add_chunk(source="s", content="X supersedes Y.")
    stats3 = _run(backfill(tree, StubExtractor()))
    assert stats3.chunks_processed == 1


def test_backfill_swallows_individual_extraction_errors(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="Alpha caused Beta.")

    class _FlakyExtractor:
        async def extract(self, _text):
            raise RuntimeError("simulated extractor failure")

    stats = _run(backfill(tree, _FlakyExtractor()))
    assert stats.chunks_processed == 0
    assert stats.chunks_skipped == 1
    # Chunk stays in the pending queue so retry can pick it up.
    assert cid in tree.chunks_pending_extraction()


# ─── Causal chain traversal ─────────────────────────────────────


def _seed_caused_chain(tree: MemoryTree) -> None:
    """A → B → C → D, each via a provenance chunk."""
    for src, dst in (("A", "B"), ("B", "C"), ("C", "D")):
        cid = tree.add_chunk(source="news", content=f"{src} caused {dst}.")
        tree.add_relation(
            src=src, dst=dst, kind="caused", confidence=0.9, provenance_chunk_id=cid
        )


def test_causal_chain_upstream_traverses_to_root(tmp_path: Path):
    tree = _tree(tmp_path)
    _seed_caused_chain(tree)
    links = causal_chain(tree, "D", depth=5, direction="upstream")
    flat = [(lk.src, lk.dst) for lk in links]
    assert ("C", "D") in flat
    assert ("B", "C") in flat
    assert ("A", "B") in flat


def test_causal_chain_downstream_walks_forward(tmp_path: Path):
    tree = _tree(tmp_path)
    _seed_caused_chain(tree)
    links = causal_chain(tree, "A", depth=5, direction="downstream")
    flat = [(lk.src, lk.dst) for lk in links]
    assert ("A", "B") in flat
    assert ("B", "C") in flat
    assert ("C", "D") in flat


def test_causal_chain_respects_depth(tmp_path: Path):
    tree = _tree(tmp_path)
    _seed_caused_chain(tree)
    links = causal_chain(tree, "D", depth=1, direction="upstream")
    pairs = {(lk.src, lk.dst) for lk in links}
    assert ("C", "D") in pairs
    assert ("B", "C") not in pairs


def test_causal_chain_returns_provenance(tmp_path: Path):
    tree = _tree(tmp_path)
    _seed_caused_chain(tree)
    links = causal_chain(tree, "B", depth=1, direction="upstream")
    assert links
    pid = links[0].provenance_chunk_id
    assert pid is not None
    chunk = tree.get_chunk(pid)
    assert chunk is not None
    assert "caused" in chunk.content


def test_causal_chain_rejects_bad_direction(tmp_path: Path):
    tree = _tree(tmp_path)
    with pytest.raises(ValueError):
        causal_chain(tree, "X", direction="sideways")


def test_causal_chain_handles_cycles(tmp_path: Path):
    tree = _tree(tmp_path)
    for src, dst in (("A", "B"), ("B", "A")):
        tree.add_relation(src=src, dst=dst, kind="caused")
    links = causal_chain(tree, "A", depth=10, direction="downstream")
    # Cycle-safe: every node visited at most once for expansion.
    pairs = {(lk.src, lk.dst) for lk in links}
    assert ("A", "B") in pairs
    assert ("B", "A") in pairs


# ─── LLMExtractor parsing ──────────────────────────────────────


def test_parse_llm_extraction_happy_path():
    payload = {
        "entities": [
            {"name": "Acme", "type": "company"},
            {"name": "Bob", "type": "person"},
        ],
        "edges": [
            {"src": "Bob", "dst": "Acme", "kind": "works_at", "confidence": 0.8},
        ],
    }
    result = parse_llm_extraction(payload)
    assert {e.name for e in result.entities} == {"Acme", "Bob"}
    assert result.edges[0].kind == "works_at"
    assert result.edges[0].confidence == pytest.approx(0.8)


def test_parse_llm_extraction_drops_unknown_kind():
    payload = {
        "entities": [{"name": "A"}],
        "edges": [
            {"src": "A", "dst": "B", "kind": "works_at"},
            {"src": "A", "dst": "C", "kind": "frobnicates"},
        ],
    }
    result = parse_llm_extraction(payload)
    kinds = [e.kind for e in result.edges]
    assert kinds == ["works_at"]


def test_parse_llm_extraction_decodes_json_string():
    s = json.dumps({"entities": [{"name": "X"}], "edges": []})
    result = parse_llm_extraction(s)
    assert result.entities[0].name == "X"


def test_parse_llm_extraction_rejects_garbage():
    with pytest.raises(ValueError):
        parse_llm_extraction("not json at all")
    with pytest.raises(TypeError):
        parse_llm_extraction(12345)
    with pytest.raises(ValueError):
        parse_llm_extraction("[1, 2, 3]")  # array, not object


def test_parse_llm_extraction_clamps_confidence():
    payload = {
        "entities": [{"name": "A"}, {"name": "B"}],
        "edges": [
            {"src": "A", "dst": "B", "kind": "caused", "confidence": 5.0},
            {"src": "A", "dst": "B", "kind": "caused", "confidence": -0.5},
        ],
    }
    result = parse_llm_extraction(payload)
    assert result.edges[0].confidence == 1.0
    assert result.edges[1].confidence == 0.0


def test_llm_extractor_dispatches_to_injected_callable():
    captured = {}

    async def fake_call(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {
            "entities": [{"name": "P"}, {"name": "Q"}],
            "edges": [{"src": "P", "dst": "Q", "kind": "caused"}],
        }

    e = LLMExtractor(llm_call=fake_call)
    res = _run(e.extract("P caused Q."))
    assert "P caused Q." in captured["prompt"]
    assert res.edges[0].kind == "caused"
