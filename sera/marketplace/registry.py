"""Marketplace registry — signed artifact catalog (P-96).

OUTCLASS: Every artifact in the registry carries its Ed25519 public key.
`install` verifies the signature before extracting. No rival ships a signed
package registry for AI skills/redpacks.

Schema
------
  packs(id, name, kind, path, pubkey_pem, description, tags, published_at, installed)
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from sera.config import SERA_HOME

REGISTRY_DB = SERA_HOME / "marketplace" / "registry.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK(kind IN ('skillpack','redpack')),
    path         TEXT NOT NULL,
    pubkey_pem   TEXT,
    description  TEXT DEFAULT '',
    tags         TEXT DEFAULT '[]',
    published_at REAL NOT NULL,
    installed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS packs_name ON packs(name);
CREATE INDEX IF NOT EXISTS packs_kind ON packs(kind);
"""

VALID_KINDS = {"skillpack", "redpack"}


@dataclass
class PackEntry:
    id: str
    name: str
    kind: str
    path: str
    pubkey_pem: str | None
    description: str
    tags: list[str]
    published_at: float
    installed: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PackEntry":
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            path=row["path"],
            pubkey_pem=row["pubkey_pem"],
            description=row["description"] or "",
            tags=json.loads(row["tags"] or "[]"),
            published_at=row["published_at"],
            installed=bool(row["installed"]),
        )


class MarketplaceRegistry:
    """SQLite-backed catalog of published and installed packs."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or REGISTRY_DB
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(
        self,
        *,
        name: str,
        kind: str,
        path: str,
        pubkey_pem: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
        now: float | None = None,
    ) -> PackEntry:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")
        if not name.strip():
            raise ValueError("name must not be empty")
        import uuid
        pack_id = uuid.uuid4().hex[:12]
        ts = float(now or time.time())
        tags_json = json.dumps(tags or [])
        self._conn.execute(
            "INSERT INTO packs (id, name, kind, path, pubkey_pem, description, "
            "tags, published_at, installed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (pack_id, name, kind, path, pubkey_pem, description, tags_json, ts),
        )
        self._conn.commit()
        return self._get_by_id(pack_id)

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def mark_installed(self, pack_id: str) -> None:
        self._conn.execute(
            "UPDATE packs SET installed = 1 WHERE id = ?", (pack_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Search / list
    # ------------------------------------------------------------------

    def search(self, query: str, kind: str | None = None) -> list[PackEntry]:
        """Case-insensitive substring search over name + description + tags."""
        q = query.lower()
        rows = self._conn.execute(
            "SELECT * FROM packs ORDER BY published_at DESC"
        ).fetchall()
        results: list[PackEntry] = []
        for row in rows:
            if kind and row["kind"] != kind:
                continue
            blob = (row["name"] + " " + (row["description"] or "") + " " + (row["tags"] or "")).lower()
            if q in blob:
                results.append(PackEntry.from_row(row))
        return results

    def list_all(self, kind: str | None = None) -> list[PackEntry]:
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM packs WHERE kind = ? ORDER BY published_at DESC", (kind,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM packs ORDER BY published_at DESC"
            ).fetchall()
        return [PackEntry.from_row(r) for r in rows]

    def list_installed(self) -> list[PackEntry]:
        rows = self._conn.execute(
            "SELECT * FROM packs WHERE installed = 1 ORDER BY published_at DESC"
        ).fetchall()
        return [PackEntry.from_row(r) for r in rows]

    def get_by_name(self, name: str, kind: str | None = None) -> PackEntry | None:
        if kind:
            row = self._conn.execute(
                "SELECT * FROM packs WHERE name = ? AND kind = ? "
                "ORDER BY published_at DESC LIMIT 1", (name, kind)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM packs WHERE name = ? "
                "ORDER BY published_at DESC LIMIT 1", (name,)
            ).fetchone()
        return PackEntry.from_row(row) if row else None

    def get_by_id(self, pack_id: str) -> PackEntry | None:
        return self._get_by_id(pack_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_by_id(self, pack_id: str) -> PackEntry:
        row = self._conn.execute(
            "SELECT * FROM packs WHERE id = ?", (pack_id,)
        ).fetchone()
        if row is None:
            raise KeyError(pack_id)
        return PackEntry.from_row(row)
