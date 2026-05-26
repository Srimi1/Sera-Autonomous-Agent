"""Tests for sera.memory.migrations — P-78 Schema evolution.

Phase verification: P-11 snapshot (v1 schema) migrates to current without data loss.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


from sera.memory.migrations.runner import Migration, MigrationRunner, MIGRATIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _runner(tmp_path: Path, migrations=None) -> MigrationRunner:
    return MigrationRunner(db=tmp_path / "mem.db", migrations=migrations)


def _inmem(migrations=None) -> MigrationRunner:
    return MigrationRunner(db=":memory:", migrations=migrations)


# ---------------------------------------------------------------------------
# MigrationRunner basics
# ---------------------------------------------------------------------------

class TestMigrationRunner:
    def test_migrate_applies_all(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        applied = runner.migrate()
        assert len(applied) == len(MIGRATIONS)

    def test_current_version_after_migrate(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate()
        assert runner.current_version() == runner.latest_version

    def test_idempotent_second_run(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate()
        second = runner.migrate()
        assert second == [], "second migrate() must return no newly applied versions"

    def test_partial_target(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        applied = runner.migrate(target=1)
        assert applied == [1]
        assert runner.current_version() == 1

    def test_incremental_apply(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate(target=1)
        applied = runner.migrate(target=2)
        assert applied == [2]
        assert runner.current_version() == 2

    def test_applied_versions_empty_initially(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        assert runner.applied_versions() == []
        assert runner.current_version() == 0

    def test_applied_versions_ordered(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate()
        vs = runner.applied_versions()
        assert vs == sorted(vs)

    def test_custom_migrations(self, tmp_path: Path) -> None:
        called = []

        def my_up(con: sqlite3.Connection) -> None:
            called.append(True)
            con.execute("CREATE TABLE IF NOT EXISTS custom_test (id INTEGER PRIMARY KEY)")

        custom = [Migration(version=1, description="custom", up=my_up)]
        runner = _runner(tmp_path, migrations=custom)
        runner.migrate()
        assert called

    def test_migration_creates_table(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate(target=1)
        con = sqlite3.connect(str(tmp_path / "mem.db"))
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        con.close()
        assert "chunks" in tables
        assert "entities" in tables
        assert "relations" in tables

    def test_v2_adds_tags_column(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate(target=2)
        con = sqlite3.connect(str(tmp_path / "mem.db"))
        cols = {r[1] for r in con.execute("PRAGMA table_info(chunks)").fetchall()}
        con.close()
        assert "tags" in cols

    def test_v3_adds_aliases_column(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate(target=3)
        con = sqlite3.connect(str(tmp_path / "mem.db"))
        cols = {r[1] for r in con.execute("PRAGMA table_info(entities)").fetchall()}
        con.close()
        assert "aliases" in cols

    def test_v4_adds_created_at(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.migrate()
        con = sqlite3.connect(str(tmp_path / "mem.db"))
        cols = {r[1] for r in con.execute("PRAGMA table_info(relations)").fetchall()}
        con.close()
        assert "created_at" in cols


# ---------------------------------------------------------------------------
# THE VERIFICATION: P-11 snapshot migrates without data loss
# ---------------------------------------------------------------------------

class TestSnapshotMigration:
    def test_v1_snapshot_migrates_to_current(self, tmp_path: Path) -> None:
        """Phase gate: create a v1 DB with data, migrate to current, data intact."""
        db_path = tmp_path / "snapshot.db"

        # Simulate a P-11 (v1) database with existing data
        con = sqlite3.connect(str(db_path))
        con.executescript("""
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0,
                embedding BLOB,
                pii_tags TEXT NOT NULL DEFAULT '[]',
                freshness REAL NOT NULL DEFAULT 1.0,
                extracted_at REAL NOT NULL,
                canonical_id INTEGER
            );
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT 'unknown'
            );
            CREATE TABLE relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_entity_id INTEGER NOT NULL,
                dst_entity_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                provenance_chunk_id INTEGER
            );
        """)
        con.execute("INSERT INTO chunks (source, content, extracted_at) VALUES ('test', 'fact A', 1.0)")
        con.execute("INSERT INTO entities (name, kind) VALUES ('Alice', 'person')")
        con.commit()
        con.close()

        # Already at v1 — tell the runner
        runner = MigrationRunner(db=db_path)
        # Manually mark v1 as applied so runner doesn't re-run it
        con2 = sqlite3.connect(str(db_path))
        con2.execute("CREATE TABLE IF NOT EXISTS _migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at REAL NOT NULL)")
        con2.execute("INSERT OR IGNORE INTO _migrations VALUES (1, 'baseline', 0.0)")
        con2.commit()
        con2.close()

        # Apply v2-v4
        applied = runner.migrate()
        assert 1 not in applied      # v1 already marked
        assert 2 in applied
        assert 3 in applied
        assert 4 in applied

        # Original data intact
        con3 = sqlite3.connect(str(db_path))
        row = con3.execute("SELECT content FROM chunks WHERE id=1").fetchone()
        assert row[0] == "fact A", "data must survive migration"
        entity = con3.execute("SELECT name FROM entities WHERE name='Alice'").fetchone()
        assert entity is not None
        con3.close()

        assert runner.current_version() == runner.latest_version
