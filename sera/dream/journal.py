"""Dream Journal — the nightly loop that makes Sera smarter while you sleep.

OUTCLASS: Kimi's blueprint *proposed* nightly consolidation; nobody shipped it.
Sera ships it. Each night the agent reviews the day's sessions and produces a
**dream entry** with three things no rival generates offline:

  1. Consolidation — a narrative summary of what happened today, so tomorrow's
     context starts from distilled memory instead of raw transcript.
  2. Candidate skills — repeated tool patterns become drafted skills (via the
     P-30 DiscoveryAgent), queued for the A/B harness to verify.
  3. Synthetic Q-A — question/answer pairs distilled from real usage. This is
     the training corpus that P-72 exports as JSONL and P-73 fine-tunes a local
     LoRA on. The flywheel: today's work becomes tomorrow's cheaper, sharper
     agent.

The loop is offline and local. The `llm_call` is injected (async, prompt → JSON
string or dict — the same shape as the curator and extractor), so the whole
journal is testable with a stub model and a fake clock, exactly like the 24h
session TTLs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Generator

from sera.config import SERA_HOME
from sera.curator.discovery import DiscoveryAgent

log = logging.getLogger("sera.dream.journal")

DREAM_DB = SERA_HOME / "dream.db"

LLMCall = Callable[[str], Awaitable[object]]


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyntheticQA:
    question: str
    answer: str
    source_session_id: str | None = None


@dataclass(frozen=True)
class DreamEntry:
    date: str                         # "2026-05-24"
    created_at: float
    summary: str
    skill_drafts: tuple[dict[str, Any], ...] = ()
    synthetic_qa: tuple[SyntheticQA, ...] = ()
    sessions_consolidated: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Prompt builders + trace
# ---------------------------------------------------------------------------

def _session_trace(session: Any) -> str:
    lines: list[str] = []
    for m in getattr(session, "messages", []):
        role = getattr(m, "role", None)
        content = getattr(m, "content", "") or ""
        if role == "user":
            lines.append(f"USER: {content}")
        elif role == "assistant":
            if content:
                lines.append(f"ASSISTANT: {content}")
            for tc in getattr(m, "tool_calls", None) or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "?"
                lines.append(f"TOOL_CALL: {name}")
        elif role == "tool":
            lines.append(f"TOOL_RESULT[{getattr(m, 'name', '?')}]: {content[:160]}")
    return "\n".join(lines)


_CONSOLIDATE_PROMPT = (
    "You are Sera's nightly consolidator. Summarize the day's sessions into a "
    "short narrative of what the user worked on, decisions made, and facts worth "
    "remembering. Reply as JSON: {\"summary\": \"...\"}.\n\nSESSIONS:\n"
)

_QA_PROMPT = (
    "From the day's sessions below, distill question/answer pairs that capture "
    "what Sera learned about the user and their work — the kind of Q-A that, if "
    "fine-tuned, would make tomorrow's agent sharper. Reply as JSON: "
    "{\"qa\": [{\"question\": \"...\", \"answer\": \"...\"}]}.\n\nSESSIONS:\n"
)


def _coerce_json(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    raise TypeError(f"llm output must be str|dict, got {type(raw).__name__}")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_entries (
    date         TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    summary      TEXT NOT NULL,
    skill_drafts TEXT NOT NULL,
    synthetic_qa TEXT NOT NULL,
    sessions_consolidated INTEGER NOT NULL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_dream_created ON dream_entries(created_at);
"""


class DreamJournalStore:
    """One persisted dream entry per night."""

    def __init__(self, *, db: Path | None = None) -> None:
        self._db = db or DREAM_DB
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def save(self, entry: DreamEntry) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO dream_entries (date, created_at, summary, skill_drafts, "
                "synthetic_qa, sessions_consolidated, error) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "created_at=excluded.created_at, summary=excluded.summary, "
                "skill_drafts=excluded.skill_drafts, synthetic_qa=excluded.synthetic_qa, "
                "sessions_consolidated=excluded.sessions_consolidated, error=excluded.error",
                (
                    entry.date, entry.created_at, entry.summary,
                    json.dumps(list(entry.skill_drafts)),
                    json.dumps([qa.__dict__ for qa in entry.synthetic_qa]),
                    entry.sessions_consolidated, entry.error,
                ),
            )
            con.commit()

    def get(self, date: str) -> DreamEntry | None:
        with self._conn() as con:
            row = con.execute("SELECT * FROM dream_entries WHERE date = ?", (date,)).fetchone()
        return self._row_to_entry(row) if row else None

    def recent(self, limit: int = 30) -> list[DreamEntry]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM dream_entries ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count(self) -> int:
        with self._conn() as con:
            return int(con.execute("SELECT COUNT(*) FROM dream_entries").fetchone()[0])

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> DreamEntry:
        qa = tuple(SyntheticQA(**d) for d in json.loads(row["synthetic_qa"]))
        return DreamEntry(
            date=row["date"],
            created_at=float(row["created_at"]),
            summary=row["summary"],
            skill_drafts=tuple(json.loads(row["skill_drafts"])),
            synthetic_qa=qa,
            sessions_consolidated=int(row["sessions_consolidated"]),
            error=row["error"],
        )


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

class DreamJournal:
    """Runs one night's dream over the day's sessions."""

    def __init__(
        self,
        *,
        store: DreamJournalStore,
        llm_call: LLMCall,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._llm = llm_call
        self._clock = clock
        self._discovery = DiscoveryAgent(llm_call)

    async def _consolidate(self, traces: str) -> str:
        if not traces.strip():
            return "(quiet day — no sessions to consolidate)"
        try:
            raw = await self._llm(_CONSOLIDATE_PROMPT + traces)
            return str(_coerce_json(raw).get("summary") or "").strip() or "(no summary)"
        except Exception as exc:  # noqa: BLE001 — consolidation is best-effort
            log.info("dream consolidation failed: %s", exc)
            return "(consolidation failed)"

    async def _synthesize_qa(self, sessions: list[Any]) -> tuple[SyntheticQA, ...]:
        traces = "\n\n".join(_session_trace(s) for s in sessions)
        if not traces.strip():
            return ()
        try:
            raw = await self._llm(_QA_PROMPT + traces)
            data = _coerce_json(raw)
        except Exception as exc:  # noqa: BLE001
            log.info("dream Q-A synthesis failed: %s", exc)
            return ()
        out: list[SyntheticQA] = []
        for item in data.get("qa") or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or "").strip()
            a = str(item.get("answer") or "").strip()
            if q and a:
                out.append(SyntheticQA(question=q, answer=a))
        return tuple(out)

    async def dream(
        self,
        *,
        date: str,
        sessions: list[Any],
        known_triggers: set[str] | frozenset[str] = frozenset(),
    ) -> DreamEntry:
        """Run one night's consolidation + discovery + Q-A, persist, return."""
        traces = "\n\n".join(_session_trace(s) for s in sessions)

        summary = await self._consolidate(traces)
        run = await self._discovery.run(sessions, known_triggers=known_triggers)
        skill_drafts = tuple(
            {
                "name": p.name,
                "trigger": p.trigger,
                "description": p.description,
                "body_hint": p.body_hint,
                "reasoning": p.reasoning,
            }
            for p in run.proposals
        )
        qa = await self._synthesize_qa(sessions)

        entry = DreamEntry(
            date=date,
            created_at=self._clock(),
            summary=summary,
            skill_drafts=skill_drafts,
            synthetic_qa=qa,
            sessions_consolidated=len(sessions),
            error=run.error,
        )
        self._store.save(entry)
        log.info("dream %s: %d sessions, %d skill drafts, %d Q-A",
                 date, len(sessions), len(skill_drafts), len(qa))
        return entry
