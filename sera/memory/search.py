"""Hybrid retrieval — fuse BM25 + vector + graph walk via Reciprocal Rank Fusion.

The fusion strategy is RRF (Cormack et al., 2009): each signal contributes
`weight / (k_rrf + rank)` to a chunk's score, summed across signals. RRF
is parameter-light, scale-invariant across heterogeneous rankers, and
robust against missing signals — a chunk seen by only one ranker still
scores; a chunk seen by all three scores highest.

Three signals:

  1. **BM25** — FTS5 `MATCH` over `chunks_fts(content, summary)`.
     Cheap, exact-term-anchored, immune to embedding drift.
  2. **Vector** — cosine over `chunks.embedding` (sqlite-vss if available,
     numpy fallback otherwise). Captures semantic similarity.
  3. **Graph walk** — find entities named in the query, collect 1-hop
     relations, return their `provenance_chunk_id`s. Surfaces chunks the
     other two signals can't reach: "the issue Alice mentioned last week"
     resolves via the entity name even when the literal terms don't match
     the chunk body.

Outclass: rivals pick one signal. Sera fuses all three; missing signals
degrade gracefully (zero weight or no embedding/entities just drops that
ranker's contribution to the RRF sum).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

from sera.memory.tree import MemoryTree

logger = logging.getLogger(__name__)

DEFAULT_RRF_K = 60
"""Standard RRF damping constant from Cormack et al.

60 keeps the rank-1 / rank-2 difference small enough that confident
multi-signal agreement beats single-signal top placement.
"""


@dataclass(frozen=True)
class HybridWeights:
    """Per-signal weight on the fused RRF score."""

    bm25: float = 1.0
    vector: float = 1.0
    graph: float = 0.5


@dataclass(frozen=True)
class HybridHit:
    chunk_id: int
    score: float
    content: str
    confidence: float
    sources: tuple[str, ...]  # signals that surfaced this chunk: bm25|vector|graph

    def __repr__(self) -> str:  # cheaper, fixed shape for debugging
        return (
            f"HybridHit(chunk_id={self.chunk_id}, score={self.score:.4f}, "
            f"sources={self.sources!r})"
        )


# ─── BM25 ──────────────────────────────────────────────────────────


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _escape_fts5(query: str) -> str:
    """Tokenize the query and OR the terms inside double-quotes.

    Each token becomes a quoted FTS5 phrase, neutralizing reserved
    operators (`:`, `*`, `AND`, `OR`, etc.) that might appear in user
    input. Joining with ` OR ` matches the chunks containing *any* term —
    natural-language queries don't read as strict AND requirements.
    Empty input falls back to an unmatchable empty phrase.
    """
    tokens = _WORD_RE.findall(query or "")
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def bm25_rank(tree: MemoryTree, query: str, *, limit: int) -> list[int]:
    """Return chunk ids ranked by FTS5 BM25 score (best first)."""
    if not (query or "").strip():
        return []
    rows = tree.conn.execute(
        "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "WHERE chunks_fts MATCH ? AND c.merged_into IS NULL "
        "ORDER BY bm25(chunks_fts) ASC LIMIT ?",
        (_escape_fts5(query), int(limit)),
    ).fetchall()
    return [int(r["id"]) for r in rows]


# ─── Vector ────────────────────────────────────────────────────────


def vector_rank(
    tree: MemoryTree, query_embedding: Sequence[float], *, limit: int
) -> list[int]:
    """Return chunk ids ranked by cosine distance to the query embedding."""
    hits = tree.search(query_embedding, limit=limit)
    return [h.chunk_id for h in hits]


# ─── Graph walk ───────────────────────────────────────────────────


def _candidate_entity_names(query: str) -> list[str]:
    """Cheap surface candidates for entity lookup.

    Yields tokens AND short capitalised noun phrases. We let the entities
    table dedupe — names that don't match anything just produce empty
    walks. The cost is one indexed query per candidate; entities is small.
    """
    if not query:
        return []
    tokens = _WORD_RE.findall(query)
    candidates: list[str] = []
    candidates.extend(tokens)
    # 2- and 3-gram windows over the token list.
    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            candidates.append(" ".join(tokens[i : i + n]))
    return candidates


def graph_neighbours(
    tree: MemoryTree, query: str, *, limit: int
) -> list[int]:
    """Return chunk ids reachable via 1-hop edges from entities in the query.

    Lookup is case-insensitive. Each relation matched contributes its
    `provenance_chunk_id`; chunks accumulate across matches so chunks
    cited by multiple relations rank higher.
    """
    if not (query or "").strip():
        return []
    seen_entities: set[int] = set()
    chunk_score: dict[int, float] = {}
    for cand in _candidate_entity_names(query):
        rows = tree.conn.execute(
            "SELECT id FROM entities WHERE LOWER(name) = LOWER(?)", (cand,)
        ).fetchall()
        for r in rows:
            eid = int(r["id"])
            if eid in seen_entities:
                continue
            seen_entities.add(eid)
            edges = tree.conn.execute(
                "SELECT confidence, provenance_chunk_id FROM relations "
                "WHERE (src_entity_id = ? OR dst_entity_id = ?) "
                "AND provenance_chunk_id IS NOT NULL",
                (eid, eid),
            ).fetchall()
            for e in edges:
                # Provenance can point at a now-merged chunk; resolve to
                # canonical so dedup'd graph hits still surface a live row.
                cid = tree.resolve_canonical(int(e["provenance_chunk_id"]))
                chunk_score[cid] = chunk_score.get(cid, 0.0) + float(e["confidence"])
    # Order by accumulated confidence, then chunk_id for stability.
    ordered = sorted(chunk_score.items(), key=lambda kv: (-kv[1], kv[0]))
    return [cid for cid, _ in ordered[:limit]]


# ─── RRF fusion ────────────────────────────────────────────────────


def _rrf_score(rank: int, *, k_rrf: int) -> float:
    """`rank` is 0-indexed. Cormack et al. style: 1 / (k + rank + 1)."""
    return 1.0 / (k_rrf + rank + 1)


@dataclass
class _Accumulator:
    score: float = 0.0
    sources: list[str] = field(default_factory=list)


def _fuse(
    rankings: dict[str, list[int]],
    *,
    weights: HybridWeights,
    k_rrf: int,
) -> list[tuple[int, float, tuple[str, ...]]]:
    """RRF-combine ranking lists. Returns [(chunk_id, score, sources), ...]."""
    weight_map = {
        "bm25": weights.bm25,
        "vector": weights.vector,
        "graph": weights.graph,
    }
    acc: dict[int, _Accumulator] = {}
    for source, ids in rankings.items():
        w = weight_map.get(source, 0.0)
        if w <= 0:
            continue
        for rank, cid in enumerate(ids):
            entry = acc.setdefault(cid, _Accumulator())
            entry.score += w * _rrf_score(rank, k_rrf=k_rrf)
            if source not in entry.sources:
                entry.sources.append(source)
    return sorted(
        ((cid, e.score, tuple(e.sources)) for cid, e in acc.items()),
        key=lambda t: (-t[1], t[0]),
    )


def hybrid_search(
    tree: MemoryTree,
    query: str,
    *,
    query_embedding: Sequence[float] | None = None,
    k: int = 10,
    weights: HybridWeights | None = None,
    k_rrf: int = DEFAULT_RRF_K,
    pool: int | None = None,
    apply_freshness: bool = True,
    touch: bool = True,
    now: float | None = None,
) -> list[HybridHit]:
    """Run BM25 + vector + graph and fuse into one ranked list.

    Each signal pulls a `pool` (default `max(k*3, 30)`) of candidates so RRF
    has enough overlap room. `query_embedding=None` skips the vector signal
    cleanly; `weights.graph=0` skips the graph walk.

    `apply_freshness=True` multiplies each fused RRF score by the chunk's
    `entity_aware_freshness` — chunks whose linked entities were recently
    mentioned stay sharp even if their own body wasn't touched. `touch=True`
    bumps the freshness of every returned hit (EWMA pull toward 1.0).
    `now` is injectable for deterministic tests; defaults to `time.time()`.

    Returns up to `k` `HybridHit`s ordered by descending fused score. Each
    hit's `sources` tuple shows which signals surfaced it — useful for
    debugging and explaining a retrieval to the user.
    """
    if k < 1:
        raise ValueError(f"k must be ≥ 1, got {k}")
    if weights is None:
        weights = HybridWeights()
    cand_limit = pool if pool is not None else max(k * 3, 30)

    rankings: dict[str, list[int]] = {}
    if weights.bm25 > 0:
        rankings["bm25"] = bm25_rank(tree, query, limit=cand_limit)
    if weights.vector > 0 and query_embedding is not None:
        rankings["vector"] = vector_rank(tree, query_embedding, limit=cand_limit)
    if weights.graph > 0:
        rankings["graph"] = graph_neighbours(tree, query, limit=cand_limit)

    fused = _fuse(rankings, weights=weights, k_rrf=k_rrf)
    if apply_freshness:
        fused = [
            (cid, score * tree.entity_aware_freshness(cid, now=now), sources)
            for cid, score, sources in fused
        ]
        fused.sort(key=lambda t: (-t[1], t[0]))
    fused = fused[:k]
    if touch:
        for cid, _score, _sources in fused:
            tree.touch_chunk(cid, now=now)
    return [_hydrate(tree, cid, score, sources) for cid, score, sources in fused]


def _hydrate(
    tree: MemoryTree, chunk_id: int, score: float, sources: tuple[str, ...]
) -> HybridHit:
    chunk = tree.get_chunk(chunk_id)
    if chunk is None:
        # Race: chunk deleted between ranking and hydration. Skip-as-empty.
        return HybridHit(
            chunk_id=chunk_id,
            score=score,
            content="",
            confidence=0.0,
            sources=sources,
        )
    return HybridHit(
        chunk_id=chunk_id,
        score=score,
        content=chunk.content,
        confidence=chunk.confidence,
        sources=sources,
    )
