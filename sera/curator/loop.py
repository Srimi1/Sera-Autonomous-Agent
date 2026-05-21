"""Post-session curator — fires after high-tool-density sessions.

Skeleton scope: count tool calls, decide if curation should run, hand a
session to a curator, persist the resulting report to a curator log.
*Mutation* of skills / memory based on proposals is deliberately out of
scope — P-23 ships the review pipeline only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as queue_mod
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from sera.config import SERA_HOME, ensure_home
from sera.memory.session import Session

logger = logging.getLogger(__name__)

ALLOWED_PROPOSAL_KINDS: tuple[str, ...] = ("skill_edit", "memory_note", "tool_hint")
"""Closed vocabulary of proposal kinds. Anything else from the LLM gets dropped.

`skill_edit` — propose changing or adding a SKILL.md.
`memory_note` — propose a long-term memory entry.
`tool_hint`  — propose a system-prompt nudge for tool selection.
"""

_REVIEW_PROMPT = (
    "You are Sera's curator. Review the transcript below and propose "
    "skill edits, memory notes, or tool hints if they would improve a "
    "future session. Output JSON of the form:\n"
    "{\n"
    "  \"proposals\": [\n"
    "    {\"kind\": <one of: skill_edit|memory_note|tool_hint>,\n"
    "     \"payload\": <object>,\n"
    "     \"reasoning\": <one short sentence>}\n"
    "  ]\n"
    "}\n"
    "Empty proposals list is fine. Do not invent capabilities not seen in "
    "the trace.\n"
    "Transcript:\n"
)

DEFAULT_TOOL_CALL_THRESHOLD = 5
"""Curator only runs when the session has STRICTLY MORE than this many
tool calls. 5 is the phase doc's `>5` — chat-heavy sessions skip review.
"""


def tool_call_count(session: Session) -> int:
    """Total tool calls made by the assistant across the session."""
    n = 0
    for m in session.messages:
        if m.role != "assistant":
            continue
        n += len(m.tool_calls or [])
    return n


def should_curate(
    session: Session, *, threshold: int = DEFAULT_TOOL_CALL_THRESHOLD
) -> bool:
    """True iff the session crossed the curation threshold."""
    return tool_call_count(session) > int(threshold)


# ─── Curator ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CuratorProposal:
    kind: str
    payload: dict[str, Any]
    reasoning: str


@dataclass(frozen=True)
class CuratorReport:
    session_id: str
    proposals: tuple[CuratorProposal, ...] = ()
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str | None = None


def _session_trace(session: Session) -> str:
    """Render a compact, LLM-friendly transcript with tool names called out."""
    lines: list[str] = []
    for m in session.messages:
        if m.role == "user":
            lines.append(f"USER: {m.content or ''}")
        elif m.role == "assistant":
            if m.content:
                lines.append(f"ASSISTANT: {m.content}")
            for tc in m.tool_calls or []:
                fn = (tc.get("function") or {})
                name = fn.get("name") or tc.get("name") or "?"
                args = fn.get("arguments") or "{}"
                lines.append(f"TOOL_CALL: {name}({args})")
        elif m.role == "tool":
            lines.append(f"TOOL_RESULT[{m.name or '?'}]: {(m.content or '')[:200]}")
    return "\n".join(lines)


@dataclass
class Curator:
    """Stateless reviewer. The `llm_call` is injected so any provider works.

    The expected `llm_call` shape mirrors P-15's `LLMExtractor`: async,
    takes a prompt string, returns either a JSON string or a parsed dict.
    Errors during JSON parse produce an empty-proposal report whose
    `.error` field records the reason (never raise to the queue).
    """

    llm_call: Callable[[str], Awaitable[object]]

    async def review(self, session: Session) -> CuratorReport:
        started = time.time()
        trace = _session_trace(session)
        try:
            raw = await self.llm_call(_REVIEW_PROMPT + trace)
            proposals = _parse_proposals(raw)
            return CuratorReport(
                session_id=session.id,
                proposals=proposals,
                started_at=started,
                finished_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001 — curator is best-effort
            logger.info("curator review failed for %s: %s", session.id, e)
            return CuratorReport(
                session_id=session.id,
                proposals=(),
                started_at=started,
                finished_at=time.time(),
                error=str(e),
            )


def _parse_proposals(raw: object) -> tuple[CuratorProposal, ...]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"curator output is not valid JSON: {e}") from e
    elif isinstance(raw, dict):
        data = raw
    else:
        raise TypeError(f"curator output must be str | dict, got {type(raw).__name__}")
    if not isinstance(data, dict):
        raise ValueError(f"curator output is not a JSON object: {data!r}")
    proposals_raw = data.get("proposals") or ()
    out: list[CuratorProposal] = []
    for p in proposals_raw:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if kind not in ALLOWED_PROPOSAL_KINDS:
            logger.info("dropping proposal with unknown kind %r", kind)
            continue
        payload = p.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
        reasoning = str(p.get("reasoning") or "")
        out.append(CuratorProposal(kind=kind, payload=payload, reasoning=reasoning))
    return tuple(out)


# ─── Curator store ──────────────────────────────────────────


CURATOR_DB = SERA_HOME / "curator.db"

_CURATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS curator_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    proposals_json TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_curator_finished ON curator_reports(finished_at);
"""


class CuratorStore:
    """Append-only SQLite log of curator reports.

    Each `record(report)` inserts one row with `proposals` serialized as
    JSON so the on-disk schema is one table — easier to query, cheap to
    migrate, and human-readable via `sqlite3 curator.db`.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else CURATOR_DB

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        ensure_home()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_CURATOR_SCHEMA)
            yield conn
        finally:
            conn.close()

    def record(self, report: "CuratorReport") -> None:
        proposals_json = json.dumps(
            [
                {
                    "kind": p.kind,
                    "payload": p.payload,
                    "reasoning": p.reasoning,
                }
                for p in report.proposals
            ]
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO curator_reports "
                "(session_id, started_at, finished_at, proposals_json, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    report.session_id,
                    float(report.started_at),
                    float(report.finished_at),
                    proposals_json,
                    report.error,
                ),
            )
            c.commit()

    def recent_reports(
        self, *, limit: int = 20
    ) -> list["CuratorReport"]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, started_at, finished_at, proposals_json, error "
                "FROM curator_reports ORDER BY finished_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out: list[CuratorReport] = []
        for r in rows:
            raw_props = json.loads(r["proposals_json"]) if r["proposals_json"] else []
            proposals = tuple(
                CuratorProposal(
                    kind=p["kind"], payload=p["payload"], reasoning=p["reasoning"]
                )
                for p in raw_props
            )
            out.append(
                CuratorReport(
                    session_id=r["session_id"],
                    proposals=proposals,
                    started_at=float(r["started_at"]),
                    finished_at=float(r["finished_at"]),
                    error=r["error"],
                )
            )
        return out


# ─── Background queue ───────────────────────────────────────


_SHUTDOWN = object()
"""Sentinel pushed onto the queue to tell the worker to exit."""


class CuratorQueue:
    """Single-thread background runner.

    `enqueue(session)` returns immediately; the worker thread runs the
    curator against the session, persists the report, and moves on. The
    main agent never blocks on curation — that's the whole outclass
    claim against Hermes's in-loop curator.

    `should_curate()` is checked at enqueue time AND at worker pickup —
    sessions that drop below threshold between calls just no-op cleanly.
    """

    def __init__(
        self,
        *,
        store: CuratorStore,
        curator_factory: Callable[[], "Curator"],
        threshold: int = DEFAULT_TOOL_CALL_THRESHOLD,
    ) -> None:
        self.store = store
        self.curator_factory = curator_factory
        self.threshold = int(threshold)
        self._queue: queue_mod.Queue = queue_mod.Queue()
        self._thread: threading.Thread | None = None
        self._idle_lock = threading.Lock()
        self._processed_event = threading.Event()
        self._processed_event.set()  # initially idle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=timeout)
        self._thread = None

    def enqueue(self, session: Session) -> bool:
        """Schedule a session for review. Returns False if below threshold."""
        if not should_curate(session, threshold=self.threshold):
            return False
        with self._idle_lock:
            self._processed_event.clear()
        self._queue.put(session)
        return True

    def wait_idle(self, *, timeout: float = 5.0) -> bool:
        """Block until every queued session has been processed."""
        return self._processed_event.wait(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                with self._idle_lock:
                    self._processed_event.set()
                return
            session: Session = item
            try:
                curator = self.curator_factory()
                report = asyncio.run(curator.review(session))
                self.store.record(report)
            except Exception as e:  # noqa: BLE001 — never kill the worker
                logger.exception("curator worker crash: %s", e)
                self.store.record(
                    CuratorReport(
                        session_id=session.id,
                        proposals=(),
                        started_at=time.time(),
                        finished_at=time.time(),
                        error=str(e),
                    )
                )
            finally:
                with self._idle_lock:
                    if self._queue.empty():
                        self._processed_event.set()
