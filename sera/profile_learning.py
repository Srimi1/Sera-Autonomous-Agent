"""Heuristic PROFILE.md suggestions with review/apply flow."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

from sera.config import SERA_HOME, ensure_home
from sera.memory.session import Session
from sera.profile import (
    MANAGED_SECTIONS,
    default_profile_sections,
    load_profile_text,
    profile_path,
    render_profile,
)

PROFILE_SUGGESTIONS_DB = SERA_HOME / "profile_suggestions.db"

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    section_key TEXT NOT NULL,
    item TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_suggestions_checksum_status
ON profile_suggestions(workspace, checksum, status);
CREATE INDEX IF NOT EXISTS idx_profile_suggestions_workspace_status
ON profile_suggestions(workspace, status, created_at DESC);
"""

_SECTION_KEYS = {key for key, _title in MANAGED_SECTIONS}

_PATTERN_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\b(terse|concise|short|brief|blunt)\b", re.I),
        "style",
        "Prefer concise, direct answers.",
    ),
    (
        re.compile(r"\b(bullets?|checklist)\b", re.I),
        "style",
        "Use bullets when the content is inherently list-shaped.",
    ),
    (
        re.compile(r"\b(no fluff|no cheerleading|skip the fluff)\b", re.I),
        "vetoes",
        "Do not add fluff, cheerleading, or motivational filler.",
    ),
    (
        re.compile(r"\b(plan first|create a plan first|give me a plan first)\b", re.I),
        "workflow",
        "Start substantial work with a concrete implementation plan.",
    ),
    (
        re.compile(r"\b(preflight|verify|verification|test it)\b", re.I),
        "workflow",
        "Include verification steps for anything reusable or risky.",
    ),
    (
        re.compile(r"\b(skill|reusable workflow|workshop)\b", re.I),
        "tooling",
        "Favor reusable skills over one-off prompt habits.",
    ),
)


@dataclass(frozen=True)
class ProfileSuggestion:
    id: int
    workspace: str
    section_key: str
    item: str
    reason: str
    source_session: str
    status: str
    checksum: str
    created_at: float
    updated_at: float


class ProfileSuggestionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else PROFILE_SUGGESTIONS_DB

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

    def create(
        self,
        *,
        workspace: str,
        section_key: str,
        item: str,
        reason: str,
        source_session: str = "",
        status: str = STATUS_PENDING,
    ) -> ProfileSuggestion:
        if section_key not in _SECTION_KEYS:
            raise ValueError(f"unknown section_key {section_key!r}")
        checksum = _checksum(section_key, item)
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM profile_suggestions "
                "WHERE workspace = ? AND checksum = ? AND status = ?",
                (workspace, checksum, status),
            ).fetchone()
            if row is None:
                cur = c.execute(
                    "INSERT INTO profile_suggestions "
                    "(workspace, section_key, item, reason, source_session, status, checksum, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        section_key,
                        item,
                        reason,
                        source_session,
                        status,
                        checksum,
                        now,
                        now,
                    ),
                )
                c.commit()
                return self.get(cast(int, cur.lastrowid))
            return _row_to_suggestion(row)

    def get(self, suggestion_id: int) -> ProfileSuggestion:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM profile_suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"suggestion {suggestion_id} not found")
        return _row_to_suggestion(row)

    def list(
        self,
        *,
        workspace: str,
        status: str | None = None,
    ) -> list[ProfileSuggestion]:
        with self._conn() as c:
            if status is None:
                rows = c.execute(
                    "SELECT * FROM profile_suggestions WHERE workspace = ? "
                    "ORDER BY created_at DESC",
                    (workspace,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM profile_suggestions WHERE workspace = ? AND status = ? "
                    "ORDER BY created_at DESC",
                    (workspace, status),
                ).fetchall()
        return [_row_to_suggestion(row) for row in rows]

    def update_status(self, suggestion_id: int, status: str) -> ProfileSuggestion:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE profile_suggestions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, suggestion_id),
            )
            c.commit()
        return self.get(suggestion_id)


class ProfileLearner:
    def __init__(
        self,
        *,
        workspace: str | Path,
        db_path: Path | None = None,
        auto_capture: bool = True,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.store = ProfileSuggestionStore(db_path=db_path)
        self.auto_capture = auto_capture

    def pending(self) -> list[ProfileSuggestion]:
        return self.store.list(workspace=self.workspace, status=STATUS_PENDING)

    def suggestions(self) -> list[ProfileSuggestion]:
        return self.store.list(workspace=self.workspace)

    def suggest(
        self,
        *,
        section_key: str,
        item: str,
        reason: str,
        source_session: str = "",
    ) -> ProfileSuggestion:
        return self.store.create(
            workspace=self.workspace,
            section_key=section_key,
            item=item.strip(),
            reason=reason.strip(),
            source_session=source_session,
        )

    async def capture_session(self, session: Session) -> list[ProfileSuggestion]:
        if not self.auto_capture:
            return []
        user_text = "\n".join(
            (m.content or "").strip()
            for m in session.messages
            if m.role == "user" and (m.content or "").strip()
        )
        if not user_text:
            return []
        found: list[ProfileSuggestion] = []
        seen_items: set[tuple[str, str]] = set()
        for pattern, section_key, item in _PATTERN_RULES:
            matches = pattern.findall(user_text)
            if not matches:
                continue
            key = (section_key, item)
            if key in seen_items:
                continue
            seen_items.add(key)
            found.append(
                self.suggest(
                    section_key=section_key,
                    item=item,
                    reason="Repeated user preference signal observed in recent messages.",
                    source_session=session.id,
                )
            )
        return found

    def apply(self, suggestion_id: int) -> ProfileSuggestion:
        suggestion = self.store.get(suggestion_id)
        if suggestion.status != STATUS_PENDING:
            raise ValueError(
                f"suggestion {suggestion_id} is {suggestion.status}, expected pending"
            )
        existing_text = load_profile_text(self.workspace)
        sections = current_profile_sections(existing_text)
        items = sections.setdefault(suggestion.section_key, [])
        if suggestion.item not in items:
            items.append(suggestion.item)
        rendered = render_profile(existing_text, sections=sections)
        profile_path(self.workspace).write_text(rendered)
        return self.store.update_status(suggestion_id, STATUS_APPLIED)

    def reject(self, suggestion_id: int) -> ProfileSuggestion:
        return self.store.update_status(suggestion_id, STATUS_REJECTED)


def current_profile_sections(existing_text: str) -> dict[str, list[str]]:
    sections = {
        key: list(values)
        for key, values in default_profile_sections().items()
    }
    text = existing_text or ""
    for key, _title in MANAGED_SECTIONS:
        pattern = re.compile(
            rf"<!-- sera:{re.escape(key)}:start -->.*?\n(.*?)<!-- sera:{re.escape(key)}:end -->",
            re.DOTALL,
        )
        match = pattern.search(text)
        if not match:
            continue
        body = match.group(1)
        items = [
            line.strip()[2:].strip()
            for line in body.splitlines()
            if line.strip().startswith("- ")
        ]
        if items:
            sections[key] = items
    return sections


def _checksum(section_key: str, item: str) -> str:
    return hashlib.sha256(f"{section_key}\n{item}".encode("utf-8")).hexdigest()


def _row_to_suggestion(row: sqlite3.Row) -> ProfileSuggestion:
    return ProfileSuggestion(
        id=int(row["id"]),
        workspace=row["workspace"],
        section_key=row["section_key"],
        item=row["item"],
        reason=row["reason"],
        source_session=row["source_session"],
        status=row["status"],
        checksum=row["checksum"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
