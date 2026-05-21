"""Memory tree — persistent long-term memory with vector recall.

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

import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import numpy as np

from sera.config import MEMORY_DB, ensure_home

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536
"""OpenAI text-embedding-3-small default. Override per MemoryTree if needed."""

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
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO chunks (source, content, summary, confidence, embedding, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (source, content, summary, float(confidence), blob, now),
        )
        chunk_id = cur.lastrowid
        if self._vss and blob is not None:
            self.conn.execute(
                "INSERT INTO chunks_vss(rowid, embedding) VALUES (?, ?)",
                (chunk_id, blob),
            )
        self.conn.commit()
        return int(chunk_id)

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

    def get_chunk(self, chunk_id: int) -> Chunk | None:
        row = self.conn.execute(
            "SELECT id, source, content, summary, confidence, created_at "
            "FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return Chunk(
            id=int(row["id"]),
            source=row["source"],
            content=row["content"],
            summary=row["summary"] or "",
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
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
        return int(cur.lastrowid)

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
        return int(cur.lastrowid)

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
    ) -> list[SearchHit]:
        """Top-k nearest chunks to `query` by cosine distance.

        Uses sqlite-vss when available; otherwise falls back to a numpy
        cosine scan over every embedded chunk. Both paths apply the
        `min_confidence` filter in SQL before ranking.
        """
        if len(query) != self.embedding_dim:
            raise ValueError(
                f"query dim {len(query)} != tree dim {self.embedding_dim}"
            )
        if self._vss:
            return self._search_vss(query, limit=limit, min_confidence=min_confidence)
        return self._search_numpy(query, limit=limit, min_confidence=min_confidence)

    def _search_vss(
        self, query: Sequence[float], *, limit: int, min_confidence: float
    ) -> list[SearchHit]:
        blob = _embedding_to_blob(query)
        rows = self.conn.execute(
            "SELECT c.id, c.content, c.confidence, v.distance "
            "FROM chunks_vss v JOIN chunks c ON c.id = v.rowid "
            "WHERE vss_search(v.embedding, vss_search_params(?, ?)) "
            "AND c.confidence >= ? "
            "ORDER BY v.distance ASC",
            (blob, limit, float(min_confidence)),
        ).fetchall()
        return [
            SearchHit(
                chunk_id=int(r["id"]),
                distance=float(r["distance"]),
                content=r["content"],
                confidence=float(r["confidence"]),
            )
            for r in rows
        ]

    def _search_numpy(
        self, query: Sequence[float], *, limit: int, min_confidence: float
    ) -> list[SearchHit]:
        q = np.asarray(query, dtype=np.float32)
        q_norm = float(np.linalg.norm(q)) or 1.0
        rows = self.conn.execute(
            "SELECT id, content, confidence, embedding FROM chunks "
            "WHERE embedding IS NOT NULL AND confidence >= ?",
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
                SearchHit(
                    chunk_id=int(r["id"]),
                    distance=distance,
                    content=r["content"],
                    confidence=float(r["confidence"]),
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
