"""Skill quality scoring — live scores in `sera skills`.

Tracks per-skill invocations, successes, failures, cost, and user thumbs.
A composite quality score drives auto-demotion out of the suggestion list.

Outclass: rivals pick skills statically or by keyword. Sera's scorer
learns from runtime outcomes — bad skills fall below threshold and stop
appearing without manual curation.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from sera.config import SERA_HOME, ensure_home

SCORES_DB = SERA_HOME / "skills_scores.db"
DEFAULT_SUGGEST_THRESHOLD = 0.35
"""Skills scoring below this are excluded from suggestions."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_scores (
    name TEXT PRIMARY KEY,
    invocations INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0.0,
    thumbs_up INTEGER NOT NULL DEFAULT 0,
    thumbs_down INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_name ON skill_scores(name);
"""


@dataclass(frozen=True)
class SkillScore:
    name: str
    invocations: int = 0
    successes: int = 0
    failures: int = 0
    total_cost: float = 0.0
    thumbs_up: int = 0
    thumbs_down: int = 0


def quality_score(s: SkillScore) -> float:
    """Composite score in [0, 1]. No invocations → 1.0 (benefit of doubt).

    Formula: 0.7 × success_rate + 0.3 × thumb_factor

    success_rate = successes / invocations (0 when no invocations → uses 1.0)
    thumb_factor = (thumbs_up - thumbs_down) / (thumbs_up + thumbs_down)
                   mapped from [-1, 1] to [0, 1]; 0 when no thumbs → 0.5
    """
    if s.invocations == 0:
        success_rate = 1.0
    else:
        success_rate = s.successes / s.invocations

    total_thumbs = s.thumbs_up + s.thumbs_down
    if total_thumbs == 0:
        thumb_factor = 0.5
    else:
        raw = (s.thumbs_up - s.thumbs_down) / total_thumbs  # [-1, 1]
        thumb_factor = (raw + 1.0) / 2.0  # [0, 1]

    return max(0.0, min(1.0, 0.7 * success_rate + 0.3 * thumb_factor))


class SkillScorer:
    """SQLite-backed per-skill quality tracker."""

    def __init__(self, db_path: Path | None = None) -> None:
        ensure_home()
        self._path = Path(db_path) if db_path is not None else SCORES_DB
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── event recording ───────────────────────────────────────────

    def record_invocation(self, name: str) -> None:
        self._upsert(name, invocations=1)

    def record_success(self, name: str) -> None:
        self._upsert(name, successes=1)

    def record_failure(self, name: str) -> None:
        self._upsert(name, failures=1)

    def record_cost(self, name: str, cost: float) -> None:
        self._upsert(name, total_cost=cost)

    def thumbs_up(self, name: str) -> None:
        self._upsert(name, thumbs_up=1)

    def thumbs_down(self, name: str) -> None:
        self._upsert(name, thumbs_down=1)

    # ── queries ───────────────────────────────────────────────────

    def get(self, name: str) -> SkillScore:
        row = self._conn.execute(
            "SELECT * FROM skill_scores WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return SkillScore(name=name)
        return SkillScore(
            name=row["name"],
            invocations=row["invocations"],
            successes=row["successes"],
            failures=row["failures"],
            total_cost=row["total_cost"],
            thumbs_up=row["thumbs_up"],
            thumbs_down=row["thumbs_down"],
        )

    def score_of(self, name: str) -> float:
        return quality_score(self.get(name))

    def should_suggest(self, name: str, threshold: float = DEFAULT_SUGGEST_THRESHOLD) -> bool:
        return self.score_of(name) >= threshold

    def demoted_skills(self, threshold: float = DEFAULT_SUGGEST_THRESHOLD) -> list[str]:
        """Return names of all tracked skills scoring below threshold."""
        rows = self._conn.execute(
            "SELECT name FROM skill_scores"
        ).fetchall()
        return [r["name"] for r in rows if self.score_of(r["name"]) < threshold]

    def all_scores(self) -> list[tuple[str, float, SkillScore]]:
        """Return [(name, score, SkillScore), ...] ordered by score desc."""
        rows = self._conn.execute(
            "SELECT name FROM skill_scores ORDER BY name"
        ).fetchall()
        result = []
        for r in rows:
            s = self.get(r["name"])
            result.append((r["name"], quality_score(s), s))
        return sorted(result, key=lambda t: -t[1])

    # ── internal ──────────────────────────────────────────────────

    def _upsert(
        self,
        name: str,
        *,
        invocations: int = 0,
        successes: int = 0,
        failures: int = 0,
        total_cost: float = 0.0,
        thumbs_up: int = 0,
        thumbs_down: int = 0,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO skill_scores
                (name, invocations, successes, failures, total_cost,
                 thumbs_up, thumbs_down, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                invocations = invocations + excluded.invocations,
                successes   = successes   + excluded.successes,
                failures    = failures    + excluded.failures,
                total_cost  = total_cost  + excluded.total_cost,
                thumbs_up   = thumbs_up   + excluded.thumbs_up,
                thumbs_down = thumbs_down + excluded.thumbs_down,
                updated_at  = excluded.updated_at
            """,
            (name, invocations, successes, failures, total_cost,
             thumbs_up, thumbs_down, now),
        )
        self._conn.commit()
