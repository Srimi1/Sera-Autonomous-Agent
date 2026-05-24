"""Migration runner — applies versioned SQLite migrations in order.

OUTCLASS: P-11's memory tree has no upgrade path.  This runner gives Sera a
safe, tested, forward-only migration system so a database from any prior
schema can be brought to the current version without data loss.

Design
------
- Migrations are plain Python functions: `def up(con: sqlite3.Connection) -> None`.
- Each migration has a monotonically increasing integer version.
- The runner stores applied versions in a `_migrations` bookkeeping table.
- Migrations are idempotent where possible (IF NOT EXISTS / OR IGNORE guards).
- Applying a version twice is a no-op (the bookkeeping table prevents re-runs).

Usage
-----
    from sera.memory.migrations.runner import MigrationRunner
    runner = MigrationRunner(db_path)
    runner.migrate()   # brings DB to current version

Testing
-------
The runner is built against an in-memory SQLite DB via the `db` parameter.
Migrations are injected via `migrations=` so tests can exercise edge cases
without touching the real schema.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator

log = logging.getLogger("sera.memory.migrations")

MigrationFn = Callable[[sqlite3.Connection], None]


# ---------------------------------------------------------------------------
# Migration catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    up: MigrationFn


def _v1_initial_schema(con: sqlite3.Connection) -> None:
    """P-11 baseline — chunks, entities, relations, FTS index."""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT    NOT NULL,
            content         TEXT    NOT NULL,
            summary         TEXT    NOT NULL DEFAULT '',
            confidence      REAL    NOT NULL DEFAULT 1.0,
            embedding       BLOB,
            pii_tags        TEXT    NOT NULL DEFAULT '[]',
            freshness       REAL    NOT NULL DEFAULT 1.0,
            extracted_at    REAL    NOT NULL,
            canonical_id    INTEGER REFERENCES chunks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source     ON chunks(source);
        CREATE INDEX IF NOT EXISTS idx_chunks_confidence ON chunks(confidence);

        CREATE TABLE IF NOT EXISTS entities (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT    NOT NULL UNIQUE,
            kind    TEXT    NOT NULL DEFAULT 'unknown'
        );

        CREATE TABLE IF NOT EXISTS relations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            src_entity_id     INTEGER NOT NULL REFERENCES entities(id),
            dst_entity_id     INTEGER NOT NULL REFERENCES entities(id),
            kind              TEXT    NOT NULL,
            confidence        REAL    NOT NULL DEFAULT 1.0,
            provenance_chunk_id INTEGER REFERENCES chunks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_relations_src  ON relations(src_entity_id);
        CREATE INDEX IF NOT EXISTS idx_relations_dst  ON relations(dst_entity_id);
        CREATE INDEX IF NOT EXISTS idx_relations_kind ON relations(kind);
    """)


def _v2_add_chunk_tags(con: sqlite3.Connection) -> None:
    """Add optional `tags` column to chunks for keyword labeling."""
    try:
        con.execute("ALTER TABLE chunks ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _v3_add_entity_aliases(con: sqlite3.Connection) -> None:
    """Add `aliases` column to entities for alternate name matching."""
    try:
        con.execute("ALTER TABLE entities ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _v4_relation_timestamp(con: sqlite3.Connection) -> None:
    """Add `created_at` to relations for freshness decay."""
    try:
        con.execute("ALTER TABLE relations ADD COLUMN created_at REAL")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


MIGRATIONS: list[Migration] = [
    Migration(version=1, description="P-11 baseline schema", up=_v1_initial_schema),
    Migration(version=2, description="chunks.tags column",   up=_v2_add_chunk_tags),
    Migration(version=3, description="entities.aliases",     up=_v3_add_entity_aliases),
    Migration(version=4, description="relations.created_at", up=_v4_relation_timestamp),
]

_CURRENT_VERSION = MIGRATIONS[-1].version


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  REAL    NOT NULL
);
"""


class MigrationRunner:
    """Applies versioned migrations to a SQLite database, forward-only."""

    def __init__(
        self,
        db: Path | str | None = None,
        migrations: list[Migration] | None = None,
    ) -> None:
        import time
        self._db = str(db) if db else ":memory:"
        self._migrations = migrations if migrations is not None else MIGRATIONS
        self._clock = time.time

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def _ensure_bookkeeping(self, con: sqlite3.Connection) -> None:
        con.executescript(_BOOKKEEPING)
        con.commit()

    def applied_versions(self) -> list[int]:
        with self._conn() as con:
            self._ensure_bookkeeping(con)
            rows = con.execute("SELECT version FROM _migrations ORDER BY version").fetchall()
        return [r["version"] for r in rows]

    def current_version(self) -> int:
        vs = self.applied_versions()
        return vs[-1] if vs else 0

    def migrate(self, target: int | None = None) -> list[int]:
        """Apply all pending migrations up to `target` (default: latest).

        Returns list of newly applied version numbers.
        """
        target = target if target is not None else _CURRENT_VERSION
        already = set(self.applied_versions())
        applied: list[int] = []

        with self._conn() as con:
            self._ensure_bookkeeping(con)
            for m in sorted(self._migrations, key=lambda x: x.version):
                if m.version > target:
                    break
                if m.version in already:
                    continue
                log.info("applying migration v%d: %s", m.version, m.description)
                m.up(con)
                con.execute(
                    "INSERT INTO _migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (m.version, m.description, self._clock()),
                )
                con.commit()
                applied.append(m.version)

        return applied

    @property
    def latest_version(self) -> int:
        return _CURRENT_VERSION
