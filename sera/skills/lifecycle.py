"""Skill lifecycle — pinned / active / stale / archived.

State machine + SQLite store. Lifecycle is tracked *separately* from the
SKILL.md manifest so a user can edit a skill without disturbing its
runtime status, and an archived skill keeps its bytes intact for revival.

Outclass: archived skills are never deleted. `revive(name)` flips them
back to ACTIVE. No rival ships first-class recovery from archive — they
either delete on stale or leave dead skills cluttering the registry.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

from sera.config import SERA_HOME, ensure_home

STALE_AFTER_SECONDS = 90 * 24 * 60 * 60
"""Idle threshold for ACTIVE → STALE auto-transition (90 days)."""

ARCHIVE_AFTER_SECONDS = 180 * 24 * 60 * 60
"""Idle threshold for STALE → archive *proposal* (180 days).

Archive itself is user-confirmed — sweep returns proposed archives,
never applies them. Reversible-with-touch transitions are auto-applied;
archive is not.
"""

LIFECYCLE_DB = SERA_HOME / "skills_lifecycle.db"


class LifecycleState(str, Enum):
    PINNED = "pinned"
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_lifecycle (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'active',
    last_used_at REAL NOT NULL,
    archived_at REAL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_state ON skill_lifecycle(state);
CREATE INDEX IF NOT EXISTS idx_lifecycle_last_used ON skill_lifecycle(last_used_at);
"""


@dataclass(frozen=True)
class LifecycleRow:
    name: str
    state: LifecycleState
    last_used_at: float
    archived_at: float | None
    pinned: bool


@dataclass(frozen=True)
class SweepSummary:
    """Result of one `sweep()` pass.

    `transitions_to_stale` is applied to the DB. `proposed_archives` is
    user-prompt fodder — sweep never auto-archives because archive is
    the one transition that needs explicit confirmation (data hides
    from search; the user should know).
    """

    transitions_to_stale: tuple[str, ...] = ()
    proposed_archives: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.transitions_to_stale or self.proposed_archives)


class SkillLifecycle:
    """Per-skill lifecycle store."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else LIFECYCLE_DB

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        ensure_home()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            yield conn
        finally:
            conn.close()

    def upsert(self, name: str, *, now: float | None = None) -> None:
        """Idempotent: insert ACTIVE+now if missing, no-op if present."""
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO skill_lifecycle "
                "(name, state, last_used_at, archived_at, pinned) "
                "VALUES (?, 'active', ?, NULL, 0)",
                (name, ts),
            )
            c.commit()

    def touch(self, name: str, *, now: float | None = None) -> None:
        """Bump `last_used_at`; insert ACTIVE row if missing.

        Equivalent to `upsert` for fresh names; for existing rows it
        moves the row out of STALE back to ACTIVE on read (the read-time
        decay in `state_of` flips the effective state without writing).
        """
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO skill_lifecycle "
                "(name, state, last_used_at, archived_at, pinned) "
                "VALUES (?, 'active', ?, NULL, 0) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  last_used_at = excluded.last_used_at, "
                "  state = CASE WHEN state = 'archived' THEN state "
                "               ELSE 'active' END",
                (name, ts),
            )
            c.commit()

    def get(self, name: str) -> LifecycleRow | None:
        """Return the raw persisted row, or None if unseen."""
        with self._conn() as c:
            row = c.execute(
                "SELECT name, state, last_used_at, archived_at, pinned "
                "FROM skill_lifecycle WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return LifecycleRow(
            name=row["name"],
            state=LifecycleState(row["state"]),
            last_used_at=float(row["last_used_at"]),
            archived_at=float(row["archived_at"]) if row["archived_at"] is not None else None,
            pinned=bool(row["pinned"]),
        )

    def state_of(self, name: str, *, now: float | None = None) -> LifecycleState:
        """Return the effective state, applying time-based decay live.

        Unseen skills implicitly read as ACTIVE — they just haven't been
        touched yet. The persisted state is *not* mutated by this read;
        `sweep()` is the only mutator for auto-transitions.
        """
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT state, last_used_at, pinned FROM skill_lifecycle "
                "WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return LifecycleState.ACTIVE
        if int(row["pinned"]):
            return LifecycleState.PINNED
        state = LifecycleState(row["state"])
        if state is LifecycleState.ARCHIVED:
            return state  # archive is sticky until revive
        idle = ts - float(row["last_used_at"])
        if idle >= STALE_AFTER_SECONDS:
            return LifecycleState.STALE
        return LifecycleState.ACTIVE

    def archive(self, name: str, *, now: float | None = None) -> None:
        """Explicit archive. Sets state + archived_at; clears `pinned`.

        Pinned status is dropped because the user just stated the
        opposite intent. Archived is sticky — `state_of` returns
        ARCHIVED until `revive()`.
        """
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO skill_lifecycle "
                "(name, state, last_used_at, archived_at, pinned) "
                "VALUES (?, 'archived', ?, ?, 0) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  state = 'archived', archived_at = excluded.archived_at, "
                "  pinned = 0",
                (name, ts, ts),
            )
            c.commit()

    def revive(self, name: str, *, now: float | None = None) -> None:
        """ARCHIVED → ACTIVE. No-op when the row doesn't exist.

        Outclass: rivals delete archived skills. Sera keeps them and
        offers a one-call revival path.
        """
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            c.execute(
                "UPDATE skill_lifecycle "
                "SET state = 'active', archived_at = NULL, last_used_at = ? "
                "WHERE name = ?",
                (ts, name),
            )
            c.commit()

    def pin(self, name: str, *, now: float | None = None) -> None:
        """Mark a skill `pinned`. Pinned skills never auto-transition."""
        ts = float(now if now is not None else time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO skill_lifecycle "
                "(name, state, last_used_at, archived_at, pinned) "
                "VALUES (?, 'active', ?, NULL, 1) "
                "ON CONFLICT(name) DO UPDATE SET pinned = 1",
                (name, ts),
            )
            c.commit()

    def unpin(self, name: str) -> None:
        """Drop pinned status. Subsequent reads apply normal decay."""
        with self._conn() as c:
            c.execute(
                "UPDATE skill_lifecycle SET pinned = 0 WHERE name = ?",
                (name,),
            )
            c.commit()

    def sweep(self, *, now: float | None = None) -> SweepSummary:
        """Apply stale auto-transitions; return archive proposals.

        STALE auto-applies (reversible). ARCHIVE never auto-applies —
        sweep returns the candidate list and the caller (CLI / curator)
        prompts the user. Skills already in ARCHIVED stay ARCHIVED.
        Skills with `pinned=1` skip every transition.
        """
        ts = float(now if now is not None else time.time())
        stale_cutoff = ts - STALE_AFTER_SECONDS
        archive_cutoff = ts - ARCHIVE_AFTER_SECONDS

        with self._conn() as c:
            # New transitions to stale: state was active AND idle past cutoff.
            stale_rows = c.execute(
                "SELECT name FROM skill_lifecycle "
                "WHERE pinned = 0 AND state = 'active' "
                "AND last_used_at <= ?",
                (stale_cutoff,),
            ).fetchall()
            transitions_to_stale = tuple(r["name"] for r in stale_rows)
            if transitions_to_stale:
                c.executemany(
                    "UPDATE skill_lifecycle SET state = 'stale' WHERE name = ?",
                    [(n,) for n in transitions_to_stale],
                )

            # Archive proposals: not pinned, not already archived,
            # idle past ARCHIVE cutoff.
            archive_rows = c.execute(
                "SELECT name FROM skill_lifecycle "
                "WHERE pinned = 0 AND state != 'archived' "
                "AND last_used_at <= ?",
                (archive_cutoff,),
            ).fetchall()
            proposed_archives = tuple(r["name"] for r in archive_rows)
            c.commit()

        return SweepSummary(
            transitions_to_stale=transitions_to_stale,
            proposed_archives=proposed_archives,
        )
