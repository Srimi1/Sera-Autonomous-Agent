"""Memory tree — persistent long-term memory with vector recall + dedup.

Schema:

  chunks      — content + summary + confidence + embedding BLOB + provenance
  entities    — deduped named things (people, projects, topics)
  relations   — typed directed edges between entities, each pointing back to
                the chunk that justifies them (provenance)
  chunks_vss  — sqlite-vss virtual table over `embedding`, when the
                extension loads. Falls back to numpy cosine otherwise.

Outclass: OpenHuman stores chunks; nobody else stores per-chunk confidence
AND per-edge confidence + provenance. Every retrieval can trace WHY the
fact is believed (provenance_chunk_id) and how strongly (confidence).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence, cast

import numpy as np

from sera.config import MEMORY_DB, ensure_home

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536
"""OpenAI text-embedding-3-small default. Override per MemoryTree if needed."""

FRESHNESS_HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
"""30 days. After this much idle time a chunk's freshness halves."""

FRESHNESS_EWMA_ALPHA = 0.5
"""How hard a touch pulls freshness back toward 1.0.

new = alpha + (1 - alpha) * decayed_old. alpha=0.5 means one touch is
worth ~half the gap between the decayed value and full freshness; a few
touches in a row pin the chunk near 1.0 again.
"""

DEFAULT_DEDUP_THRESHOLD = 0.95


def _pii_redaction_notice(pii_tags: Iterable[str]) -> str:
    """Standard notice returned in place of content when consent gate blocks read."""
    return (
        f"[redacted — pii: {','.join(pii_tags)}; "
        f"pass consent=True to reveal]"
    )
"""Cosine similarity ≥ this collapses a new chunk into the existing canonical.

0.95 is empirically conservative — distinct paraphrases of the same fact
land around 0.85-0.92; near-identical re-ingestion lands above 0.97. The
threshold is tunable per add_or_merge call.
"""

_VSS_AVAILABLE = False
_VSS_CHECKED = False


def _try_load_vss(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vss extension into `conn`.

    Returns True iff load succeeded. Caches the result in `_VSS_AVAILABLE`
    so we only probe the import once per process — repeated probes after a
    failure waste cycles.
    """
    global _VSS_AVAILABLE, _VSS_CHECKED
    if _VSS_CHECKED:
        if _VSS_AVAILABLE:
            try:
                import sqlite_vss

                conn.enable_load_extension(True)
                sqlite_vss.load(conn)
                conn.enable_load_extension(False)
                return True
            except Exception:  # noqa: BLE001 — extension load is best-effort
                return False
        return False
    _VSS_CHECKED = True
    try:
        import sqlite_vss

        conn.enable_load_extension(True)
        sqlite_vss.load(conn)
        conn.enable_load_extension(False)
        _VSS_AVAILABLE = True
        return True
    except Exception as e:  # noqa: BLE001
        logger.info("sqlite-vss unavailable; using numpy cosine fallback (%s)", e)
        _VSS_AVAILABLE = False
        return False


@dataclass(frozen=True)
class Chunk:
    id: int
    source: str
    content: str
    summary: str
    confidence: float
    created_at: float
    pii_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entity:
    id: int
    name: str
    type: str
    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class Relation:
    id: int
    src_entity_id: int
    dst_entity_id: int
    kind: str
    confidence: float
    provenance_chunk_id: int | None
    created_at: float


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    distance: float
    content: str
    confidence: float
    pii_tags: tuple[str, ...] = ()
    redacted: bool = False


def _decayed(
    *, stored: float, last_seen: float | None, now: float, half_life: float
) -> float:
    """Exponential decay of `stored` from `last_seen` to `now`.

    `last_seen` None or in the future is treated as "no decay" — return
    `stored` unchanged but clamped to [0, 1]. `half_life` ≤ 0 is invalid
    upstream so we don't guard it here.
    """
    if stored <= 0.0:
        return 0.0
    if last_seen is None or now <= float(last_seen):
        return min(1.0, stored)
    elapsed = float(now) - float(last_seen)
    # True half-life: value halves every `half_life` seconds.
    factor = 0.5 ** (elapsed / float(half_life))
    return max(0.0, min(1.0, stored * factor))


def _embedding_to_blob(vec: Sequence[float]) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class MemoryTree:
    """SQLite-backed memory store with vector recall.

    Connection is opened lazily and lives for the tree's lifetime. The
    sqlite-vss extension is probed once on first connect; if absent,
    `search` falls back to a numpy-cosine scan over all chunk embeddings.
    The fallback is O(N) per query — fine up to ~10k chunks.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        self.db_path = db_path or MEMORY_DB
        self.embedding_dim = embedding_dim
        self._conn: sqlite3.Connection | None = None
        self._vss: bool = False

    # ─── Connection lifecycle ────────────────────────────────────────

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def _open(self) -> None:
        ensure_home()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        self._vss = _try_load_vss(c)
        self._apply_schema(c)
        self._conn = c

    def _apply_schema(self, c: sqlite3.Connection) -> None:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                embedding BLOB,
                extracted_at REAL,
                freshness REAL NOT NULL DEFAULT 1.0,
                last_accessed_at REAL,
                merged_into INTEGER REFERENCES chunks(id),
                merged_from TEXT,
                pii_tags TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
            CREATE INDEX IF NOT EXISTS idx_chunks_confidence ON chunks(confidence);
            -- idx_chunks_extracted_at is created in the migration block
            -- below so legacy DBs (no extracted_at column yet) don't fail
            -- the schema script.

            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_entity_id INTEGER NOT NULL REFERENCES entities(id),
                dst_entity_id INTEGER NOT NULL REFERENCES entities(id),
                kind TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                provenance_chunk_id INTEGER REFERENCES chunks(id),
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_entity_id);
            CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(dst_entity_id);
            CREATE INDEX IF NOT EXISTS idx_relations_kind ON relations(kind);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content, summary, content='chunks', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, content, summary)
                VALUES (new.id, COALESCE(new.content, ''), COALESCE(new.summary, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, summary)
                VALUES ('delete', old.id, COALESCE(old.content, ''),
                        COALESCE(old.summary, ''));
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, summary)
                VALUES ('delete', old.id, COALESCE(old.content, ''),
                        COALESCE(old.summary, ''));
                INSERT INTO chunks_fts(rowid, content, summary)
                VALUES (new.id, COALESCE(new.content, ''),
                        COALESCE(new.summary, ''));
            END;
            """
        )
        if self._vss:
            c.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vss USING vss0("
                f"  embedding({self.embedding_dim})"
                f")"
            )
        # Idempotent migration for older DBs that pre-date extracted_at.
        existing = {r[1] for r in c.execute("PRAGMA table_info(chunks)").fetchall()}
        legacy_db = "extracted_at" not in existing
        if legacy_db:
            c.execute("ALTER TABLE chunks ADD COLUMN extracted_at REAL")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_extracted_at "
                "ON chunks(extracted_at)"
            )
        if "freshness" not in existing:
            c.execute(
                "ALTER TABLE chunks ADD COLUMN freshness REAL NOT NULL DEFAULT 1.0"
            )
        if "last_accessed_at" not in existing:
            c.execute("ALTER TABLE chunks ADD COLUMN last_accessed_at REAL")
        if "merged_into" not in existing:
            c.execute("ALTER TABLE chunks ADD COLUMN merged_into INTEGER")
        if "merged_from" not in existing:
            c.execute("ALTER TABLE chunks ADD COLUMN merged_from TEXT")
        if "pii_tags" not in existing:
            c.execute("ALTER TABLE chunks ADD COLUMN pii_tags TEXT")

        # Backfill chunks_fts. The triggers above only fire on future
        # writes — pre-existing chunks need an explicit rebuild via FTS5's
        # external-content command. We always rebuild on a legacy upgrade
        # (extracted_at was just added → this is the first connect with
        # the FTS schema). For non-legacy DBs the COUNT(*) on chunks_fts
        # is unreliable (FTS5 internal rows can inflate it), so we fall
        # back to "any chunks at all without FTS entries" as the trigger.
        chunks_count = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        needs_rebuild = legacy_db or (
            chunks_count > 0
            and c.execute(
                "SELECT COUNT(*) FROM chunks WHERE id NOT IN "
                "(SELECT rowid FROM chunks_fts)"
            ).fetchone()[0]
            > 0
        )
        if needs_rebuild:
            c.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        c.commit()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def session(self) -> Iterator["MemoryTree"]:
        try:
            yield self
        finally:
            self.close()

    @property
    def using_vss(self) -> bool:
        """True iff vector search is delegated to sqlite-vss (not numpy)."""
        # Trigger lazy open so callers get the actual answer.
        _ = self.conn
        return self._vss

    # ─── Chunks ──────────────────────────────────────────────────────

    def add_chunk(
        self,
        *,
        source: str,
        content: str,
        summary: str = "",
        confidence: float = 1.0,
        embedding: Sequence[float] | None = None,
        now: float | None = None,
    ) -> int:
        """Insert a chunk. Returns its `id`.

        Confidence is clamped to [0, 1]. If `embedding` is supplied its
        length must match `embedding_dim` — mismatched dims would break
        the vss index silently.
        """
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence out of range: {confidence}")
        if embedding is not None and len(embedding) != self.embedding_dim:
            raise ValueError(
                f"embedding dim {len(embedding)} != tree dim {self.embedding_dim}"
            )

        blob = _embedding_to_blob(embedding) if embedding is not None else None
        now_ts = float(now if now is not None else time.time())
        from sera.memory.privacy import pii_kinds as _pii_kinds

        tags = _pii_kinds(content)
        tags_json = json.dumps(tags) if tags else None
        cur = self.conn.execute(
            "INSERT INTO chunks (source, content, summary, confidence, embedding, "
            "freshness, last_accessed_at, pii_tags, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?)",
            (source, content, summary, float(confidence), blob, now_ts,
             tags_json, now_ts),
        )
        chunk_id = cur.lastrowid
        if self._vss and blob is not None:
            self.conn.execute(
                "INSERT INTO chunks_vss(rowid, embedding) VALUES (?, ?)",
                (chunk_id, blob),
            )
        self.conn.commit()
        return cast(int, chunk_id)  # lastrowid is always set after INSERT

    def update_chunk(
        self,
        chunk_id: int,
        *,
        content: str | None = None,
        summary: str | None = None,
        confidence: float | None = None,
        embedding: Sequence[float] | None = None,
    ) -> bool:
        """Partial-update a chunk. Returns True iff the row existed.

        Only non-None fields are written. The vss virtual table is kept
        in sync — if a new embedding is supplied we delete + re-insert
        the vss row (vss0 doesn't support UPDATE).
        """
        row = self.conn.execute(
            "SELECT id FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            return False

        sets: list[str] = []
        params: list[object] = []
        if content is not None:
            sets.append("content = ?")
            params.append(content)
            # Re-tag PII whenever the body changes — the previous tag set is
            # invalid once the text differs.
            from sera.memory.privacy import pii_kinds as _pii_kinds

            tags = _pii_kinds(content)
            sets.append("pii_tags = ?")
            params.append(json.dumps(tags) if tags else None)
        if summary is not None:
            sets.append("summary = ?")
            params.append(summary)
        if confidence is not None:
            if not 0.0 <= confidence <= 1.0:
                raise ValueError(f"confidence out of range: {confidence}")
            sets.append("confidence = ?")
            params.append(float(confidence))
        new_blob: bytes | None = None
        if embedding is not None:
            if len(embedding) != self.embedding_dim:
                raise ValueError(
                    f"embedding dim {len(embedding)} != tree dim {self.embedding_dim}"
                )
            new_blob = _embedding_to_blob(embedding)
            sets.append("embedding = ?")
            params.append(new_blob)
        if not sets:
            return True  # nothing to write but the row exists

        params.append(chunk_id)
        self.conn.execute(
            f"UPDATE chunks SET {', '.join(sets)} WHERE id = ?", params
        )
        if self._vss and new_blob is not None:
            # vss0 has no UPDATE — replace the indexed row.
            self.conn.execute("DELETE FROM chunks_vss WHERE rowid = ?", (chunk_id,))
            self.conn.execute(
                "INSERT INTO chunks_vss(rowid, embedding) VALUES (?, ?)",
                (chunk_id, new_blob),
            )
        self.conn.commit()
        return True

    def mark_extracted(self, chunk_id: int, *, when: float | None = None) -> bool:
        """Stamp `extracted_at` on a chunk. Returns True iff the row existed."""
        ts = float(when if when is not None else time.time())
        cur = self.conn.execute(
            "UPDATE chunks SET extracted_at = ? WHERE id = ?",
            (ts, chunk_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def chunks_pending_extraction(self, *, limit: int = 100) -> list[int]:
        """IDs of chunks that have never been run through entity extraction.

        Ordered by id so backfill walks insertions chronologically.
        """
        rows = self.conn.execute(
            "SELECT id FROM chunks WHERE extracted_at IS NULL "
            "ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def delete_chunk(self, chunk_id: int) -> bool:
        """Remove a chunk plus its vss row. Returns True iff a row was deleted."""
        cur = self.conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        deleted = cur.rowcount > 0
        if deleted and self._vss:
            self.conn.execute("DELETE FROM chunks_vss WHERE rowid = ?", (chunk_id,))
        self.conn.commit()
        return deleted

    # ─── Dedup / consolidation ───────────────────────────────────────

    def resolve_canonical(self, chunk_id: int) -> int:
        """Follow `merged_into` pointers to the canonical chunk.

        Cycle-safe via a visited set — a misconfigured pointer chain
        breaks cleanly rather than looping forever. Returns the same id
        if the chunk is already canonical or doesn't exist.
        """
        visited: set[int] = set()
        cur = int(chunk_id)
        while cur not in visited:
            visited.add(cur)
            row = self.conn.execute(
                "SELECT merged_into FROM chunks WHERE id = ?", (cur,)
            ).fetchone()
            if row is None or row["merged_into"] is None:
                return cur
            cur = int(row["merged_into"])
        return cur

    def find_near_duplicate(
        self,
        embedding: Sequence[float],
        *,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> tuple[int, float] | None:
        """Return (canonical_id, cosine_similarity) for the best match above
        `threshold`, or None.

        Uses the configured vector backend; threshold is on cosine
        similarity in [0, 1]. The vector search itself already skips
        merged-into rows, so the returned id is always canonical.
        """
        hits = self.search(embedding, limit=1)
        if not hits:
            return None
        similarity = 1.0 - float(hits[0].distance)
        if similarity >= float(threshold):
            return (int(hits[0].chunk_id), similarity)
        return None

    def add_or_merge_chunk(
        self,
        *,
        source: str,
        content: str,
        summary: str = "",
        confidence: float = 1.0,
        embedding: Sequence[float] | None = None,
        now: float | None = None,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> tuple[int, bool]:
        """Insert a chunk OR merge into an existing near-duplicate.

        Returns (chunk_id, merged) where `merged` indicates whether the
        new content collapsed into an existing canonical. On merge:
          * `merged_from` on the canonical gains a JSON entry recording
            the source label that produced the duplicate.
          * `confidence` is bumped to `max(existing, new)`.
          * canonical is touched (freshness EWMA reset).

        Without an embedding we can't compute similarity → always insert.
        """
        if embedding is None:
            new_id = self.add_chunk(
                source=source,
                content=content,
                summary=summary,
                confidence=confidence,
                embedding=None,
                now=now,
            )
            return new_id, False

        match = self.find_near_duplicate(embedding, threshold=dedup_threshold)
        if match is None:
            new_id = self.add_chunk(
                source=source,
                content=content,
                summary=summary,
                confidence=confidence,
                embedding=embedding,
                now=now,
            )
            return new_id, False

        canonical_id, similarity = match
        existing = self.conn.execute(
            "SELECT confidence, merged_from FROM chunks WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        merged_from = json.loads(existing["merged_from"]) if existing["merged_from"] else []
        merged_from.append(
            {"source": source, "similarity": similarity, "at": float(now or time.time())}
        )
        new_confidence = max(float(existing["confidence"] or 0.0), float(confidence))
        self.conn.execute(
            "UPDATE chunks SET merged_from = ?, confidence = ? WHERE id = ?",
            (json.dumps(merged_from), new_confidence, canonical_id),
        )
        self.conn.commit()
        self.touch_chunk(canonical_id, now=now)
        return canonical_id, True

    def merged_from_for(self, chunk_id: int) -> list[dict]:
        """Read the JSON provenance list for a canonical chunk."""
        row = self.conn.execute(
            "SELECT merged_from FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None or not row["merged_from"]:
            return []
        return list(json.loads(row["merged_from"]))

    # ─── Freshness ───────────────────────────────────────────────────

    def freshness_of(
        self,
        chunk_id: int,
        *,
        now: float | None = None,
        half_life: float = FRESHNESS_HALF_LIFE_SECONDS,
    ) -> float:
        """Time-decayed freshness of a chunk *without* touching it.

        Reads stored `freshness` + `last_accessed_at`; applies exponential
        decay from `last_accessed_at` to `now`. Returns 0.0 if the chunk
        doesn't exist. Pure read — no UPDATE.
        """
        row = self.conn.execute(
            "SELECT freshness, last_accessed_at FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return 0.0
        return _decayed(
            stored=float(row["freshness"] or 0.0),
            last_seen=row["last_accessed_at"],
            now=now if now is not None else time.time(),
            half_life=half_life,
        )

    def touch_chunk(
        self,
        chunk_id: int,
        *,
        now: float | None = None,
        half_life: float = FRESHNESS_HALF_LIFE_SECONDS,
        alpha: float = FRESHNESS_EWMA_ALPHA,
    ) -> float:
        """Mark a chunk as accessed; bumps freshness via EWMA toward 1.0.

        Combines two effects:
          1. Decay since `last_accessed_at` (exponential, governed by
             `half_life`).
          2. EWMA pull-toward-1.0 (`new = alpha + (1 - alpha) * decayed`).

        Returns the new stored freshness. No-op on missing chunks
        (returns 0.0).
        """
        now_ts = float(now if now is not None else time.time())
        row = self.conn.execute(
            "SELECT freshness, last_accessed_at FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return 0.0
        decayed = _decayed(
            stored=float(row["freshness"] or 0.0),
            last_seen=row["last_accessed_at"],
            now=now_ts,
            half_life=half_life,
        )
        new = alpha + (1.0 - alpha) * decayed
        new = max(0.0, min(1.0, new))
        self.conn.execute(
            "UPDATE chunks SET freshness = ?, last_accessed_at = ? WHERE id = ?",
            (new, now_ts, chunk_id),
        )
        self.conn.commit()
        return new

    def entity_aware_freshness(
        self,
        chunk_id: int,
        *,
        now: float | None = None,
        half_life: float = FRESHNESS_HALF_LIFE_SECONDS,
    ) -> float:
        """Effective freshness: max(direct decay, decayed-entity-last_seen).

        A 2-year-old chunk linked (via provenance) to an entity mentioned
        yesterday stays sharp. Outclass: rivals decay docs uniformly;
        Sera weights recency by which entities are still alive.
        """
        now_ts = float(now if now is not None else time.time())
        base = self.freshness_of(chunk_id, now=now_ts, half_life=half_life)
        row = self.conn.execute(
            "SELECT MAX(e.last_seen) AS m FROM relations r "
            "JOIN entities e ON (e.id = r.src_entity_id OR e.id = r.dst_entity_id) "
            "WHERE r.provenance_chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None or row["m"] is None:
            return base
        elapsed = max(0.0, now_ts - float(row["m"]))
        entity_decay = 0.5 ** (elapsed / float(half_life))
        return max(base, entity_decay)

    def get_chunk(self, chunk_id: int, *, consent: bool = False) -> Chunk | None:
        """Return a chunk by id. Redacts content when consent=False + pii_tags.

        Default `consent=False` is the security contract: every direct caller
        states intent explicitly. Internal callers that own the read context
        (vault sync writing the user's own files, internal stats) pass
        `consent=True` with a comment justifying it. The extractor path
        (`graph.extract_and_persist`) keeps the default — chunk content
        never leaks into LLM traces.
        """
        row = self.conn.execute(
            "SELECT id, source, content, summary, confidence, created_at, pii_tags "
            "FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        tags_raw = row["pii_tags"]
        tags = tuple(json.loads(tags_raw)) if tags_raw else ()
        content = row["content"]
        if tags and not consent:
            content = _pii_redaction_notice(tags)
        return Chunk(
            id=int(row["id"]),
            source=row["source"],
            content=content,
            summary=row["summary"] or "",
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
            pii_tags=tags,
        )

    # ─── Entities ────────────────────────────────────────────────────

    def add_entity(self, *, name: str, type: str) -> int:
        """Upsert by name. Returns entity id.

        Bumps `last_seen` on every call so the most recently referenced
        entities surface first in freshness-aware queries (P-17).
        """
        now = time.time()
        existing = self.conn.execute(
            "SELECT id FROM entities WHERE name = ?", (name,)
        ).fetchone()
        if existing is not None:
            self.conn.execute(
                "UPDATE entities SET last_seen = ? WHERE id = ?",
                (now, existing["id"]),
            )
            self.conn.commit()
            return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO entities (name, type, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (name, type, now, now),
        )
        self.conn.commit()
        return cast(int, cur.lastrowid)  # lastrowid is always set after INSERT

    def get_entity(self, entity_id: int) -> Entity | None:
        row = self.conn.execute(
            "SELECT id, name, type, first_seen, last_seen FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return Entity(
            id=int(row["id"]),
            name=row["name"],
            type=row["type"],
            first_seen=float(row["first_seen"]),
            last_seen=float(row["last_seen"]),
        )

    def find_entity(self, name: str) -> Entity | None:
        row = self.conn.execute(
            "SELECT id, name, type, first_seen, last_seen FROM entities WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Entity(
            id=int(row["id"]),
            name=row["name"],
            type=row["type"],
            first_seen=float(row["first_seen"]),
            last_seen=float(row["last_seen"]),
        )

    # ─── Relations ───────────────────────────────────────────────────

    def add_relation(
        self,
        *,
        src: str,
        dst: str,
        kind: str,
        confidence: float = 1.0,
        provenance_chunk_id: int | None = None,
        src_type: str = "concept",
        dst_type: str = "concept",
    ) -> int:
        """Create a typed edge between two entities (upserted by name)."""
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence out of range: {confidence}")
        src_id = self.add_entity(name=src, type=src_type)
        dst_id = self.add_entity(name=dst, type=dst_type)
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO relations (src_entity_id, dst_entity_id, kind, "
            "confidence, provenance_chunk_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src_id, dst_id, kind, float(confidence), provenance_chunk_id, now),
        )
        self.conn.commit()
        return cast(int, cur.lastrowid)  # lastrowid is always set after INSERT

    def relations_for(self, entity_name: str) -> list[Relation]:
        """All outgoing edges from a named entity."""
        rows = self.conn.execute(
            "SELECT r.id, r.src_entity_id, r.dst_entity_id, r.kind, r.confidence, "
            "r.provenance_chunk_id, r.created_at "
            "FROM relations r JOIN entities e ON e.id = r.src_entity_id "
            "WHERE e.name = ? ORDER BY r.id ASC",
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

    # ─── Vector search ───────────────────────────────────────────────

    def search(
        self,
        query: Sequence[float],
        *,
        limit: int = 10,
        min_confidence: float = 0.0,
        consent: bool = False,
    ) -> list[SearchHit]:
        """Top-k nearest chunks to `query` by cosine distance.

        Default `consent=False` redacts SearchHit.content when the chunk
        carries pii_tags. This is the security contract: direct callers
        of tree.search must state consent explicitly. Hybrid search,
        vault sync, and other internal id-only consumers pass
        `consent=True` to skip the redaction work since they consume
        chunk_id (not content) or have already gated upstream.
        """
        if len(query) != self.embedding_dim:
            raise ValueError(
                f"query dim {len(query)} != tree dim {self.embedding_dim}"
            )
        if self._vss:
            return self._search_vss(
                query, limit=limit, min_confidence=min_confidence, consent=consent,
            )
        return self._search_numpy(
            query, limit=limit, min_confidence=min_confidence, consent=consent,
        )

    def _hit_for_row(
        self,
        *,
        chunk_id: int,
        content: str,
        confidence: float,
        distance: float,
        pii_tags_raw: object,
        consent: bool,
    ) -> SearchHit:
        tags = (
            tuple(json.loads(pii_tags_raw)) if pii_tags_raw else ()  # type: ignore[arg-type]
        )
        hit_content = content
        redacted = False
        if tags and not consent:
            hit_content = _pii_redaction_notice(tags)
            redacted = True
        return SearchHit(
            chunk_id=chunk_id,
            distance=distance,
            content=hit_content,
            confidence=confidence,
            pii_tags=tags,
            redacted=redacted,
        )

    def _search_vss(
        self,
        query: Sequence[float],
        *,
        limit: int,
        min_confidence: float,
        consent: bool,
    ) -> list[SearchHit]:
        blob = _embedding_to_blob(query)
        rows = self.conn.execute(
            "SELECT c.id, c.content, c.confidence, c.pii_tags, v.distance "
            "FROM chunks_vss v JOIN chunks c ON c.id = v.rowid "
            "WHERE vss_search(v.embedding, vss_search_params(?, ?)) "
            "AND c.confidence >= ? "
            "AND c.merged_into IS NULL "
            "ORDER BY v.distance ASC",
            (blob, limit, float(min_confidence)),
        ).fetchall()
        return [
            self._hit_for_row(
                chunk_id=int(r["id"]),
                content=r["content"],
                confidence=float(r["confidence"]),
                distance=float(r["distance"]),
                pii_tags_raw=r["pii_tags"],
                consent=consent,
            )
            for r in rows
        ]

    def _search_numpy(
        self,
        query: Sequence[float],
        *,
        limit: int,
        min_confidence: float,
        consent: bool,
    ) -> list[SearchHit]:
        q = np.asarray(query, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) or 1.0
        rows = self.conn.execute(
            "SELECT id, content, confidence, embedding, pii_tags FROM chunks "
            "WHERE embedding IS NOT NULL AND confidence >= ? "
            "AND merged_into IS NULL",
            (float(min_confidence),),
        ).fetchall()
        hits: list[SearchHit] = []
        for r in rows:
            vec = _blob_to_embedding(r["embedding"])
            if vec.shape[0] != q.shape[0]:
                continue  # malformed row, skip rather than crash search
            v_norm = float(np.linalg.norm(vec)) or 1.0
            cosine = float(np.dot(q, vec) / (q_norm * v_norm))
            # vss returns L2-style "distance" (smaller is closer). Map cosine
            # similarity → distance via 1 - cosine so both backends agree on
            # ordering semantics.
            distance = 1.0 - cosine
            hits.append(
                self._hit_for_row(
                    chunk_id=int(r["id"]),
                    content=r["content"],
                    confidence=float(r["confidence"]),
                    distance=distance,
                    pii_tags_raw=r["pii_tags"],
                    consent=consent,
                )
            )
        hits.sort(key=lambda h: (h.distance, -h.confidence))
        return hits[:limit]

    # ─── Diagnostics ────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        """Row counts for the three core tables (used by /test + CLI)."""
        out: dict[str, int] = {}
        for tbl in ("chunks", "entities", "relations"):
            out[tbl] = int(
                self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            )
        return out


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """Standalone helper for callers without a MemoryTree handle."""
    va = np.asarray(list(a), dtype=np.float32)
    vb = np.asarray(list(b), dtype=np.float32)
    na = float(np.linalg.norm(va)) or 1.0
    nb = float(np.linalg.norm(vb)) or 1.0
    return float(np.dot(va, vb) / (na * nb))


def euclidean_distance(a: Iterable[float], b: Iterable[float]) -> float:
    """Standalone helper for callers without a MemoryTree handle."""
    va = np.asarray(list(a), dtype=np.float32)
    vb = np.asarray(list(b), dtype=np.float32)
    return float(math.sqrt(float(np.sum((va - vb) ** 2))))
