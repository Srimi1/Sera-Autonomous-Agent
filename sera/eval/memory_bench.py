"""Retrieval recall benchmark.

Inputs: a corpus (yaml list of chunks) + a queries file (yaml list of
{id, query, expected_ids, entities?}). Builds an ephemeral `MemoryTree`,
ingests every chunk, runs each query under each retrieval mode, and
reports MRR + Recall@k + median latency.

Modes:
  * `vector` — embedding-only via `tree.search`.
  * `bm25`   — FTS5 via `bm25_rank`.
  * `graph`  — entity-walk via `graph_neighbours`.
  * `hybrid` — RRF fusion (default for production).

The benchmark is the release gate: hybrid MRR must beat the single-signal
modes, and the hybrid number is what we publish.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

import yaml

from sera.memory.embedder import Embedder, StubEmbedder
from sera.memory.graph import StubExtractor, extract_and_persist
from sera.memory.search import (
    HybridWeights,
    bm25_rank,
    graph_neighbours,
    hybrid_search,
    vector_rank,
)
from sera.memory.tree import MemoryTree

DEFAULT_TOP_K = 10


# ─── Dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class RecallCase:
    """One query + the ids that should appear in the top hits."""

    id: str
    query: str
    expected_ids: tuple[int, ...]


@dataclass
class BenchResult:
    """One mode's aggregate numbers across all queries."""

    mode: str
    mrr: float
    recall_at: dict[int, float]
    median_latency_ms: float
    queries: int

    def as_row(self) -> list[str]:
        r1 = self.recall_at.get(1, 0.0)
        r5 = self.recall_at.get(5, 0.0)
        r10 = self.recall_at.get(10, 0.0)
        return [
            self.mode,
            f"{self.mrr:.3f}",
            f"{r1:.2f}",
            f"{r5:.2f}",
            f"{r10:.2f}",
            f"{self.median_latency_ms:.1f}",
            str(self.queries),
        ]


@dataclass
class RecallCorpus:
    chunks: list[dict] = field(default_factory=list)


# ─── Math helpers ──────────────────────────────────────────────


def mrr(rankings: Iterable[Sequence[int]], expected: Iterable[Sequence[int]]) -> float:
    """Mean Reciprocal Rank.

    For each query, finds the rank (1-indexed) of the first hit that's in
    the expected set. Missing match contributes 0. The metric is the
    arithmetic mean across queries.
    """
    rankings = list(rankings)
    expected = list(expected)
    if not rankings or not expected:
        return 0.0
    if len(rankings) != len(expected):
        raise ValueError("rankings and expected must align 1:1 per query")
    total = 0.0
    for ranked, exp in zip(rankings, expected):
        exp_set = set(exp)
        rank = 0.0
        for i, cid in enumerate(ranked, start=1):
            if cid in exp_set:
                rank = 1.0 / i
                break
        total += rank
    return total / len(rankings)


def recall_at(
    rankings: Iterable[Sequence[int]],
    expected: Iterable[Sequence[int]],
    k: int,
) -> float:
    """Fraction of queries with at least one expected id in the top-k."""
    if k < 1:
        raise ValueError(f"k must be ≥ 1, got {k}")
    rankings = list(rankings)
    expected = list(expected)
    if not rankings:
        return 0.0
    if len(rankings) != len(expected):
        raise ValueError("rankings and expected must align 1:1 per query")
    hits = 0
    for ranked, exp in zip(rankings, expected):
        exp_set = set(exp)
        top = ranked[:k]
        if any(cid in exp_set for cid in top):
            hits += 1
    return hits / len(rankings)


# ─── Loaders ───────────────────────────────────────────────────


def load_corpus(path: Path) -> RecallCorpus:
    """YAML schema: top-level `chunks:` list of {id?, source, content, entities?}.

    `id` is optional in the file — the bench assigns sequential ids when
    the file omits them, and re-maps `expected_ids` accordingly during
    ingest. For test stability the bundled file specifies them explicitly.
    """
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    chunks = raw.get("chunks") or []
    if not isinstance(chunks, list):
        raise ValueError(f"corpus {path} is missing a 'chunks:' list")
    return RecallCorpus(chunks=chunks)


def load_queries(path: Path) -> list[RecallCase]:
    """YAML schema: top-level `queries:` list of {id, query, expected_ids}."""
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    queries = raw.get("queries") or []
    out: list[RecallCase] = []
    for q in queries:
        out.append(
            RecallCase(
                id=str(q["id"]),
                query=q["query"],
                expected_ids=tuple(int(x) for x in (q.get("expected_ids") or ())),
            )
        )
    return out


# ─── Runner ────────────────────────────────────────────────────


async def _ingest_corpus(
    tree: MemoryTree, corpus: RecallCorpus, embedder: Embedder
) -> dict[int, int]:
    """Insert every chunk; return file-id → tree-id mapping.

    The corpus file's `id` field is purely a key for the queries to
    reference — the underlying sqlite id may differ. We return the map
    so query expected_ids can be translated downstream.
    """
    mapping: dict[int, int] = {}
    extractor = StubExtractor()
    for c in corpus.chunks:
        file_id = int(c["id"])
        content = c["content"]
        embedding = await embedder.embed(content)
        new_id = tree.add_chunk(
            source=c.get("source", "bench"),
            content=content,
            embedding=embedding,
        )
        mapping[file_id] = new_id
        await extract_and_persist(tree, new_id, extractor)
        for entity in c.get("entities") or ():
            tree.add_entity(name=str(entity), type="concept")
            tree.add_relation(
                src=str(entity), dst="bench",
                kind="mentions",
                provenance_chunk_id=new_id,
            )
    return mapping


async def _async_rank(
    tree: MemoryTree,
    embedder: Embedder,
    case: RecallCase,
    mode: str,
    top_k: int,
) -> tuple[list[int], float]:
    """Async variant — avoids `run_until_complete` from inside an event loop."""
    started = time.perf_counter()
    if mode == "bm25":
        ranked = bm25_rank(tree, case.query, limit=top_k)
    elif mode == "vector":
        qvec = await embedder.embed(case.query)
        ranked = vector_rank(tree, qvec, limit=top_k)
    elif mode == "graph":
        ranked = graph_neighbours(tree, case.query, limit=top_k)
    elif mode == "hybrid":
        qvec = await embedder.embed(case.query)
        ranked = [
            h.chunk_id
            for h in hybrid_search(
                tree, case.query,
                query_embedding=qvec, k=top_k,
                touch=False, apply_freshness=False, consent=True,
            )
        ]
    else:
        raise ValueError(f"unknown mode {mode!r}")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return ranked, elapsed_ms


def run_memory_bench(
    corpus_path: Path,
    queries_path: Path,
    *,
    embedder: Embedder | None = None,
    embedding_dim: int = 64,
    modes: Sequence[str] = ("vector", "bm25", "graph", "hybrid"),
    top_k: int = DEFAULT_TOP_K,
    tree_db_path: Path | None = None,
) -> list[BenchResult]:
    """Run the bench over every (mode, query). Returns per-mode aggregates.

    `tree_db_path=None` builds an in-memory-like temp DB; pass a real path
    if you want to inspect the populated tree after the run.
    """
    embedder = embedder or StubEmbedder(dim=embedding_dim)
    corpus = load_corpus(corpus_path)
    queries = load_queries(queries_path)

    async def _run() -> list[BenchResult]:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sera-bench-") as td:
            db_path = tree_db_path or Path(td) / "bench.db"
            tree = MemoryTree(db_path=db_path, embedding_dim=embedding_dim)
            file_to_tree = await _ingest_corpus(tree, corpus, embedder)

            # Translate expected_ids (file-space) into tree-space.
            translated_expected: list[tuple[int, ...]] = []
            for q in queries:
                translated_expected.append(
                    tuple(file_to_tree[fid] for fid in q.expected_ids if fid in file_to_tree)
                )

            out: list[BenchResult] = []
            for mode in modes:
                rankings: list[list[int]] = []
                latencies: list[float] = []
                for q in queries:
                    ranked, ms = await _async_rank(tree, embedder, q, mode, top_k)
                    rankings.append(ranked)
                    latencies.append(ms)
                out.append(
                    BenchResult(
                        mode=mode,
                        mrr=mrr(rankings, translated_expected),
                        recall_at={
                            1: recall_at(rankings, translated_expected, 1),
                            5: recall_at(rankings, translated_expected, 5),
                            10: recall_at(rankings, translated_expected, 10),
                        },
                        median_latency_ms=float(median(latencies) if latencies else 0.0),
                        queries=len(queries),
                    )
                )
            tree.close()
            return out

    return asyncio.run(_run())


# ─── HybridWeights re-export so callers can tune defaults ──────


__all__ = [
    "BenchResult",
    "HybridWeights",
    "RecallCase",
    "RecallCorpus",
    "load_corpus",
    "load_queries",
    "mrr",
    "recall_at",
    "run_memory_bench",
]
