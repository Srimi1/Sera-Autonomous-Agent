"""Skill Workshop — capture reusable procedures as pending workspace skills."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

from sera.config import SERA_HOME, ensure_home, workspace_skills_dir
from sera.memory.session import Session
from sera.skills.lifecycle import SkillLifecycle
from sera.skills.loader import get_default_registry, load_skill
from sera.skills.scaffold import SkillScaffoldResult, scaffold_skill, slugify_skill_name
from sera.skills.verify import VerificationReport, load_replay_cases, verify_via_replay

WORKSHOP_DB = SERA_HOME / "skill_workshop.db"

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_QUARANTINED = "quarantined"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_workshop_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    description TEXT NOT NULL,
    body TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workshop_checksum_pending
ON skill_workshop_proposals(checksum, status);
CREATE INDEX IF NOT EXISTS idx_workshop_status_created
ON skill_workshop_proposals(status, created_at DESC);
"""

_CUE_PATTERNS = (
    re.compile(r"\bnext time\b", re.I),
    re.compile(r"\bwhen asked\b", re.I),
    re.compile(r"\balways\b", re.I),
    re.compile(r"\bremember to\b", re.I),
    re.compile(r"\bworkflow\b", re.I),
    re.compile(r"\bchecklist\b", re.I),
    re.compile(r"\bbefore using\b", re.I),
    re.compile(r"\bverify\b", re.I),
)

_RISK_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bapi[_ -]?key\b", re.I),
    re.compile(r"\btoken\b", re.I),
    re.compile(r"\bpassword\b", re.I),
    re.compile(r"\bsecret\b", re.I),
    re.compile(r"\bcredential\b", re.I),
    re.compile(r"\bauthorization\b", re.I),
    re.compile(r"\bdelete database\b", re.I),
)


@dataclass(frozen=True)
class SkillProposal:
    id: int
    skill_name: str
    title: str
    reason: str
    description: str
    body: str
    source_session: str
    risk_level: str
    status: str
    checksum: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class AppliedSkill:
    proposal: SkillProposal
    scaffold: SkillScaffoldResult
    verification: VerificationReport

    @property
    def verified(self) -> bool:
        return self.verification.passed


class SkillWorkshopStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else WORKSHOP_DB

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
        skill_name: str,
        title: str,
        reason: str,
        description: str,
        body: str,
        source_session: str = "",
        risk_level: str,
        status: str,
    ) -> SkillProposal:
        checksum = _proposal_checksum(skill_name, body)
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM skill_workshop_proposals "
                "WHERE checksum = ? AND status = ?",
                (checksum, status),
            ).fetchone()
            if row is None:
                cur = c.execute(
                    "INSERT INTO skill_workshop_proposals "
                    "(skill_name, title, reason, description, body, source_session, "
                    " risk_level, status, checksum, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_name,
                        title,
                        reason,
                        description,
                        body,
                        source_session,
                        risk_level,
                        status,
                        checksum,
                        now,
                        now,
                    ),
                )
                proposal_id = cast(int, cur.lastrowid)
                c.commit()
                return self.get(proposal_id)
            return _row_to_proposal(row)

    def get(self, proposal_id: int) -> SkillProposal:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM skill_workshop_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"proposal {proposal_id} not found")
        return _row_to_proposal(row)

    def list(self, *, status: str | None = None) -> list[SkillProposal]:
        with self._conn() as c:
            if status is None:
                rows = c.execute(
                    "SELECT * FROM skill_workshop_proposals "
                    "ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM skill_workshop_proposals "
                    "WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def update_status(self, proposal_id: int, status: str) -> SkillProposal:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE skill_workshop_proposals "
                "SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, proposal_id),
            )
            c.commit()
        return self.get(proposal_id)


class SkillWorkshop:
    def __init__(
        self,
        *,
        workspace: str | Path,
        auto_capture: bool = True,
        approval_policy: str = STATUS_PENDING,
        review_mode: str = "heuristic",
        db_path: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.skills_root = workspace_skills_dir(self.workspace)
        self.auto_capture = auto_capture
        self.approval_policy = approval_policy
        self.review_mode = review_mode
        self.store = SkillWorkshopStore(db_path=db_path)
        self.lifecycle = SkillLifecycle()

    @classmethod
    def from_config(
        cls,
        cfg: dict,
        *,
        workspace: str | Path,
        db_path: Path | None = None,
    ) -> "SkillWorkshop":
        workshop_cfg = ((cfg.get("skills") or {}).get("workshop") or {})
        return cls(
            workspace=workspace,
            auto_capture=bool(workshop_cfg.get("auto_capture", True)),
            approval_policy=str(workshop_cfg.get("approval_policy", STATUS_PENDING)),
            review_mode=str(workshop_cfg.get("review_mode", "heuristic")),
            db_path=db_path,
        )

    def suggest(
        self,
        *,
        skill_name: str,
        title: str,
        reason: str,
        description: str,
        body: str,
        source_session: str = "",
    ) -> SkillProposal:
        slug = slugify_skill_name(skill_name)
        risk_level = _classify_risk("\n".join((title, reason, description, body)))
        status = STATUS_PENDING if risk_level == "low" else STATUS_QUARANTINED
        return self.store.create(
            skill_name=slug,
            title=title.strip(),
            reason=reason.strip(),
            description=description.strip(),
            body=body.strip(),
            source_session=source_session,
            risk_level=risk_level,
            status=status,
        )

    def pending(self) -> list[SkillProposal]:
        return self.store.list(status=STATUS_PENDING)

    def proposals(self) -> list[SkillProposal]:
        return self.store.list()

    def reject(self, proposal_id: int) -> SkillProposal:
        return self.store.update_status(proposal_id, STATUS_REJECTED)

    async def apply(self, proposal_id: int, *, force: bool = False) -> AppliedSkill:
        proposal = self.store.get(proposal_id)
        if proposal.status != STATUS_PENDING:
            raise ValueError(
                f"proposal {proposal_id} is {proposal.status}, expected pending"
            )

        scaffold = scaffold_skill(
            self.skills_root,
            name=proposal.skill_name,
            description=proposal.description,
            body=proposal.body,
            force=force,
        )
        self.lifecycle.mark_candidate(proposal.skill_name)
        skill = load_skill(scaffold.skill_path)
        report = await verify_via_replay(
            self.lifecycle,
            skill,
            load_replay_cases(scaffold.replay_path),
        )
        reg = get_default_registry(self.skills_root, lifecycle=self.lifecycle)
        reg.refresh()
        self.store.update_status(proposal_id, STATUS_APPLIED)
        return AppliedSkill(
            proposal=self.store.get(proposal_id),
            scaffold=scaffold,
            verification=report,
        )

    async def capture_session(self, session: Session) -> SkillProposal | None:
        if not self.auto_capture or self.review_mode == "off":
            return None
        suggestion = _derive_session_proposal(session)
        if suggestion is None:
            return None
        return self.suggest(
            skill_name=suggestion["skill_name"],
            title=suggestion["title"],
            reason=suggestion["reason"],
            description=suggestion["description"],
            body=suggestion["body"],
            source_session=session.id,
        )


def _proposal_checksum(skill_name: str, body: str) -> str:
    return hashlib.sha256(f"{skill_name}\n{body}".encode("utf-8")).hexdigest()


def _row_to_proposal(row: sqlite3.Row) -> SkillProposal:
    return SkillProposal(
        id=int(row["id"]),
        skill_name=row["skill_name"],
        title=row["title"],
        reason=row["reason"],
        description=row["description"],
        body=row["body"],
        source_session=row["source_session"],
        risk_level=row["risk_level"],
        status=row["status"],
        checksum=row["checksum"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _classify_risk(text: str) -> str:
    haystack = text.strip()
    for pattern in _RISK_PATTERNS:
        if pattern.search(haystack):
            return "high"
    return "low"


def _derive_session_proposal(session: Session) -> dict[str, str] | None:
    user_texts = [
        (m.content or "").strip()
        for m in session.messages
        if m.role == "user" and (m.content or "").strip()
    ]
    if not user_texts:
        return None

    recent = "\n".join(user_texts[-3:])
    lines = [line.strip() for line in recent.splitlines() if line.strip()]
    durable = [line for line in lines if any(p.search(line) for p in _CUE_PATTERNS)]
    assistant_tool_calls = sum(
        len(m.tool_calls or [])
        for m in session.messages
        if m.role == "assistant"
    )
    if not durable and assistant_tool_calls < 2:
        return None

    workflow_lines = _workflow_lines(durable or lines)
    if not workflow_lines:
        return None

    skill_name = _infer_skill_name(recent)
    title = _title_from_skill_name(skill_name)
    description = f"Reusable workflow captured from session {session.id}."
    reason = "User established a repeatable workflow worth preserving as a skill."
    body = _render_body(title, workflow_lines)
    return {
        "skill_name": skill_name,
        "title": title,
        "reason": reason,
        "description": description,
        "body": body,
    }


def _workflow_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        cleaned = re.sub(
            r"^(next time|remember to|always|when asked|before using)\b[:, -]*",
            "",
            line,
            flags=re.I,
        ).strip()
        cleaned = cleaned.rstrip(".")
        if cleaned and cleaned not in out:
            out.append(cleaned[:180])
    return out[:6]


def _infer_skill_name(text: str) -> str:
    lowered = text.lower()
    if "gif" in lowered or "animated" in lowered:
        return "animated-gif-workflow"
    if "screenshot" in lowered or "png" in lowered:
        return "screenshot-asset-workflow"
    if "skill" in lowered and "workshop" in lowered:
        return "skill-workshop-workflow"
    words = re.findall(r"[a-z0-9]+", lowered)
    filtered = [w for w in words if w not in {"next", "time", "when", "asked", "always", "verify", "workflow"}]
    return slugify_skill_name("-".join(filtered[:4]) or "captured-workflow")


def _title_from_skill_name(skill_name: str) -> str:
    return skill_name.replace("-", " ").title()


def _render_body(title: str, workflow_lines: list[str]) -> str:
    bullets = "\n".join(f"- {line}" for line in workflow_lines)
    return (
        f"# {title}\n\n"
        "Use this workflow when the same request comes back.\n\n"
        f"{bullets}\n"
        "- Verify the final result before responding.\n"
    )
