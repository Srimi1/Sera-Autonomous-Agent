"""Per-chunk entity + typed-edge extraction.

For every chunk in the memory tree, an `Extractor` produces:
  * named entities (people, projects, concepts) — already deduped by name
    upstream, but each extractor emits its best-guess type label
  * typed edges between those entities, each with a confidence in [0, 1]

Edge kinds are a closed vocabulary (`EDGE_KINDS`) so the downstream graph
walker can reason about causality without a free-text-relation explosion.

Two extractor implementations:
  * `StubExtractor` — pure regex matching on canonical verb forms. Zero
    deps, deterministic, no network. Used by tests and offline runs.
  * `LLMExtractor` — JSON-mode prompt against the configured LLM. Returns
    the same `ExtractionResult` shape; the agent loop and the backfill
    pass treat both equivalently.

Persistence is via `extract_and_persist`: entities upserted by name,
edges written with `provenance_chunk_id` so every fact is traceable.

Outclass: nobody on the rivals list ships *typed causal* edges with
per-edge confidence + provenance. "What caused X" is a 1-query BFS over
the `caused`-kind subgraph, not a free-text grep.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Protocol, Sequence

from sera.memory.tree import MemoryTree, Relation

logger = logging.getLogger(__name__)

EDGE_KINDS: tuple[str, ...] = (
    "mentions",
    "works_at",
    "parent_of",
    "caused",
    "refuted_by",
    "supersedes",
    "similar_to",
)
"""Closed vocabulary of typed edges. Anything else is rejected at write time."""


class UnknownEdgeKind(ValueError):
    """Raised when a caller (or LLM extraction) emits an edge kind outside
    `EDGE_KINDS`. We refuse free-text relations so the graph stays queryable.
    """


def _validate_kind(kind: str) -> str:
    if kind not in EDGE_KINDS:
        raise UnknownEdgeKind(
            f"edge kind {kind!r} not in {EDGE_KINDS}; refusing to persist"
        )
    return kind


# ─── Result dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    type: str = "concept"


@dataclass(frozen=True)
class ExtractedEdge:
    src: str
    dst: str
    kind: str
    confidence: float = 1.0


@dataclass(frozen=True)
class ExtractionResult:
    entities: tuple[ExtractedEntity, ...] = ()
    edges: tuple[ExtractedEdge, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entities) or bool(self.edges)


# ─── Protocol ──────────────────────────────────────────────────────


class Extractor(Protocol):
    async def extract(self, text: str) -> ExtractionResult: ...


# ─── Stub (regex) ──────────────────────────────────────────────────


# Each pattern: `src verb dst`. We capture short noun-phrases (1-3 words,
# capitalized so we hit Named Entities not generic determiners). Lowercase
# entity names so the dedup key is stable across mentions.
_NOUN = r"([A-Z][A-Za-z0-9_-]+(?:\s+[A-Z][A-Za-z0-9_-]+){0,2})"

_VERB_PATTERNS: tuple[tuple[str, str], ...] = (
    ("caused", rf"{_NOUN}\s+caused\s+{_NOUN}"),
    ("refuted_by", rf"{_NOUN}\s+was\s+refuted\s+by\s+{_NOUN}"),
    ("refuted_by", rf"{_NOUN}\s+refuted\s+by\s+{_NOUN}"),
    ("supersedes", rf"{_NOUN}\s+supersedes\s+{_NOUN}"),
    ("similar_to", rf"{_NOUN}\s+is\s+similar\s+to\s+{_NOUN}"),
    ("works_at", rf"{_NOUN}\s+works\s+at\s+{_NOUN}"),
    ("parent_of", rf"{_NOUN}\s+is\s+(?:the\s+)?parent\s+of\s+{_NOUN}"),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(rx)) for kind, rx in _VERB_PATTERNS
)


def _normalize_name(s: str) -> str:
    return " ".join(s.split())


@dataclass
class StubExtractor:
    """Pure-Python extractor for tests + offline use.

    Matches a small set of canonical verb forms. Anything outside the
    pattern set goes unrecognized — by design. The stub is for testing
    the *pipeline*, not for production retrieval quality.
    """

    confidence: float = 0.7

    async def extract(self, text: str) -> ExtractionResult:
        entities: dict[str, ExtractedEntity] = {}
        edges: list[ExtractedEdge] = []
        for kind, pattern in _COMPILED:
            for m in pattern.finditer(text):
                src = _normalize_name(m.group(1))
                dst = _normalize_name(m.group(2))
                entities[src] = ExtractedEntity(name=src)
                entities[dst] = ExtractedEntity(name=dst)
                edges.append(
                    ExtractedEdge(src=src, dst=dst, kind=kind, confidence=self.confidence)
                )
        return ExtractionResult(
            entities=tuple(entities.values()),
            edges=tuple(edges),
        )


# ─── LLM-driven ────────────────────────────────────────────────────


_LLM_PROMPT = (
    "Extract named entities and typed relationships from the text below.\n"
    "Allowed edge kinds (strict): "
    + ", ".join(EDGE_KINDS)
    + ".\n"
    "Return JSON of the form:\n"
    "{\n"
    "  \"entities\": [{\"name\": str, \"type\": str}],\n"
    "  \"edges\": [{\"src\": str, \"dst\": str, \"kind\": str, "
    "\"confidence\": float}]\n"
    "}\n"
    "Confidence is in [0, 1]. Use canonical full names where possible.\n"
    "Do not invent facts not stated in the text.\n"
    "Text:\n"
)


@dataclass
class LLMExtractor:
    """Calls a JSON-mode LLM and parses the structured response.

    The LLM is injected so the same class works against any provider —
    OpenAI, Anthropic, or a local model — as long as it returns parseable
    JSON. Malformed responses raise; the caller decides whether to skip
    or retry the offending chunk.
    """

    llm_call: Callable[[str], Awaitable[object]]

    async def extract(self, text: str) -> ExtractionResult:
        raw = await self.llm_call(_LLM_PROMPT + text)
        return parse_llm_extraction(raw)


def parse_llm_extraction(raw: object) -> ExtractionResult:
    """Validate a JSON-shaped extraction. Raises on schema violations.

    Accepts a JSON string (will be decoded) or an already-parsed dict.
    Skips edges whose kind isn't in `EDGE_KINDS` rather than raising —
    one bad edge shouldn't tank a whole extraction.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"extractor output is not valid JSON: {e}") from e
    elif isinstance(raw, dict):
        data = raw
    else:
        raise TypeError(f"expected JSON string or dict, got {type(raw).__name__}")

    if not isinstance(data, dict):
        raise ValueError(f"extractor output is not a JSON object: {data!r}")

    raw_entities: Iterable[dict] = data.get("entities") or ()
    raw_edges: Iterable[dict] = data.get("edges") or ()

    entities: list[ExtractedEntity] = []
    for e in raw_entities:
        if not isinstance(e, dict):
            continue
        name = (e.get("name") or "").strip()
        if not name:
            continue
        entities.append(
            ExtractedEntity(name=_normalize_name(name), type=e.get("type") or "concept")
        )

    edges: list[ExtractedEdge] = []
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind not in EDGE_KINDS:
            logger.info("dropping edge with unknown kind %r", kind)
            continue
        src = (e.get("src") or "").strip()
        dst = (e.get("dst") or "").strip()
        if not src or not dst:
            continue
        try:
            confidence = float(e.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence))
        edges.append(
            ExtractedEdge(
                src=_normalize_name(src),
                dst=_normalize_name(dst),
                kind=kind,
                confidence=confidence,
            )
        )

    return ExtractionResult(entities=tuple(entities), edges=tuple(edges))


# ─── Persistence + backfill ────────────────────────────────────────


@dataclass(frozen=True)
class ExtractStats:
    chunks_processed: int = 0
    entities_written: int = 0
    edges_written: int = 0
    chunks_skipped: int = 0


async def extract_and_persist(
    tree: MemoryTree,
    chunk_id: int,
    extractor: Extractor,
) -> ExtractionResult:
    """Run the extractor on one chunk, persist entities + edges, stamp `extracted_at`.

    Edge kinds are validated against `EDGE_KINDS` before insert so the
    persisted graph stays in the closed vocabulary even if a custom
    extractor went off-script.
    """
    chunk = tree.get_chunk(chunk_id)
    if chunk is None:
        raise KeyError(f"chunk {chunk_id} not found")
    result = await extractor.extract(chunk.content)
    for entity in result.entities:
        tree.add_entity(name=entity.name, type=entity.type or "concept")
    for edge in result.edges:
        _validate_kind(edge.kind)
        tree.add_relation(
            src=edge.src,
            dst=edge.dst,
            kind=edge.kind,
            confidence=edge.confidence,
            provenance_chunk_id=chunk_id,
        )
    tree.mark_extracted(chunk_id)
    return result


async def backfill(
    tree: MemoryTree,
    extractor: Extractor,
    *,
    limit: int = 100,
) -> ExtractStats:
    """Process every chunk lacking `extracted_at`. Idempotent across runs."""
    ids = tree.chunks_pending_extraction(limit=limit)
    processed = entities_written = edges_written = skipped = 0
    for cid in ids:
        try:
            result = await extract_and_persist(tree, cid, extractor)
            processed += 1
            entities_written += len(result.entities)
            edges_written += len(result.edges)
        except Exception as e:  # noqa: BLE001 — one bad chunk shouldn't halt backfill
            logger.exception("extraction failed for chunk %s: %s", cid, e)
            skipped += 1
    return ExtractStats(
        chunks_processed=processed,
        entities_written=entities_written,
        edges_written=edges_written,
        chunks_skipped=skipped,
    )


# ─── Causal traversal ──────────────────────────────────────────────


@dataclass(frozen=True)
class CausalLink:
    """One step in a causal chain — `src` caused `dst`, with provenance."""

    src: str
    dst: str
    confidence: float
    provenance_chunk_id: int | None


def causal_chain(
    tree: MemoryTree,
    entity_name: str,
    *,
    depth: int = 3,
    direction: str = "upstream",
) -> list[CausalLink]:
    """Walk `caused` edges and return the chain in BFS order.

    `direction`:
      * "upstream" — what caused `entity_name`? Follow incoming `caused`
        edges (other → entity).
      * "downstream" — what did `entity_name` cause? Follow outgoing.

    Cycle-safe: a name appearing twice in the visited set short-circuits.
    """
    if direction not in {"upstream", "downstream"}:
        raise ValueError(f"direction must be upstream|downstream, got {direction!r}")
    visited: set[str] = {entity_name}
    frontier: list[str] = [entity_name]
    out: list[CausalLink] = []
    for _ in range(max(0, depth)):
        next_frontier: list[str] = []
        for name in frontier:
            edges = _caused_neighbors(tree, name, direction=direction)
            for edge in edges:
                other = (
                    _entity_name(tree, edge.src_entity_id)
                    if direction == "upstream"
                    else _entity_name(tree, edge.dst_entity_id)
                )
                if other is None:
                    continue
                link = (
                    CausalLink(
                        src=other,
                        dst=name,
                        confidence=edge.confidence,
                        provenance_chunk_id=edge.provenance_chunk_id,
                    )
                    if direction == "upstream"
                    else CausalLink(
                        src=name,
                        dst=other,
                        confidence=edge.confidence,
                        provenance_chunk_id=edge.provenance_chunk_id,
                    )
                )
                out.append(link)
                if other not in visited:
                    visited.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier:
            break
    return out


def _entity_name(tree: MemoryTree, entity_id: int) -> str | None:
    row = tree.conn.execute(
        "SELECT name FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    return row["name"] if row else None


def _caused_neighbors(
    tree: MemoryTree, entity_name: str, *, direction: str
) -> Sequence[Relation]:
    """All `caused` edges pointing into / out of an entity."""
    if direction == "upstream":
        rows = tree.conn.execute(
            "SELECT r.id, r.src_entity_id, r.dst_entity_id, r.kind, r.confidence, "
            "r.provenance_chunk_id, r.created_at "
            "FROM relations r JOIN entities e ON e.id = r.dst_entity_id "
            "WHERE e.name = ? AND r.kind = 'caused' ORDER BY r.id ASC",
            (entity_name,),
        ).fetchall()
    else:
        rows = tree.conn.execute(
            "SELECT r.id, r.src_entity_id, r.dst_entity_id, r.kind, r.confidence, "
            "r.provenance_chunk_id, r.created_at "
            "FROM relations r JOIN entities e ON e.id = r.src_entity_id "
            "WHERE e.name = ? AND r.kind = 'caused' ORDER BY r.id ASC",
            (entity_name,),
        ).fetchall()
    return [
        Relation(
            id=int(r["id"]),
            src_entity_id=int(r["src_entity_id"]),
            dst_entity_id=int(r["dst_entity_id"]),
            kind=r["kind"],
            confidence=float(r["confidence"]),
            provenance_chunk_id=(
                int(r["provenance_chunk_id"])
                if r["provenance_chunk_id"] is not None
                else None
            ),
            created_at=float(r["created_at"]),
        )
        for r in rows
    ]
