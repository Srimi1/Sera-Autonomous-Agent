"""Cost ceilings — per-day / per-session / per-skill.

Hard caps refuse the turn before the LLM call.
Soft caps emit a warning banner but allow the turn.
Bills never surprise.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Raised by run_turn when a hard cost cap is hit."""


# ---------------------------------------------------------------------------
# Status taxonomy
# ---------------------------------------------------------------------------

class BudgetStatus(enum.Enum):
    OK = "ok"
    SoftWarning = "soft_warning"
    HardBlock = "hard_block"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class BudgetConfig:
    """Cost limits loaded from config['budget']. All values in USD."""
    session_soft_usd: float = 0.50
    session_hard_usd: float = 1.00
    day_soft_usd: float = 2.00
    day_hard_usd: float = 5.00
    # task_kind → (soft_usd, hard_usd); empty = no per-skill limits
    skill_limits: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "BudgetConfig":
        b = cfg.get("budget", {})
        skill_raw = b.get("skill_limits", {})
        skill_limits: dict[str, tuple[float, float]] = {
            k: (float(v[0]), float(v[1])) for k, v in skill_raw.items()
        }
        return cls(
            session_soft_usd=float(b.get("session_soft_usd", 0.50)),
            session_hard_usd=float(b.get("session_hard_usd", 1.00)),
            day_soft_usd=float(b.get("day_soft_usd", 2.00)),
            day_hard_usd=float(b.get("day_hard_usd", 5.00)),
            skill_limits=skill_limits,
        )


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------

@dataclass
class BudgetCheck:
    status: BudgetStatus
    message: str | None
    spent_session: float
    spent_day: float
    spent_skill: dict[str, float]

    @property
    def ok(self) -> bool:
        return self.status == BudgetStatus.OK

    @property
    def blocked(self) -> bool:
        return self.status == BudgetStatus.HardBlock

    @property
    def warning(self) -> bool:
        return self.status == BudgetStatus.SoftWarning


# ---------------------------------------------------------------------------
# Enforcer
# ---------------------------------------------------------------------------

class BudgetEnforcer:
    """Tracks accumulated spend and enforces soft/hard caps.

    Session spend: accumulated in-memory via add().
    Day spend:     queried from router_stats DB (completed calls only).
    Skill spend:   in-memory per task_kind via add().

    Usage pattern (in loop.py):
        enforcer.check(task_kind).blocked → raise BudgetExceeded
        ... do LLM call ...
        enforcer.add(cost, task_kind=kind)
    """

    def __init__(self, config: BudgetConfig, *, _db=None) -> None:
        self._cfg = config
        self._db = _db  # override for testing
        self._session_start: float = time.time()
        self._session_spent: float = 0.0
        self._skill_spent: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def add(self, cost_usd: float, *, task_kind: str = "chat") -> None:
        """Record cost after a completed LLM call."""
        self._session_spent += cost_usd
        self._skill_spent[task_kind] = self._skill_spent.get(task_kind, 0.0) + cost_usd

    def reset_session(self) -> None:
        """Reset session counters when starting a new session."""
        self._session_start = time.time()
        self._session_spent = 0.0
        self._skill_spent.clear()

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def check(self, *, task_kind: str = "chat") -> BudgetCheck:
        """Evaluate all limits. Returns HardBlock, SoftWarning, or OK."""
        spent_day = self._day_spent()
        spent_skill = dict(self._skill_spent)
        skill_soft, skill_hard = self._cfg.skill_limits.get(task_kind, (None, None))

        # Hard caps — check most specific first
        if skill_hard is not None and spent_skill.get(task_kind, 0.0) >= skill_hard:
            return BudgetCheck(
                status=BudgetStatus.HardBlock,
                message=(
                    f"Turn blocked: {task_kind} cost ${spent_skill.get(task_kind, 0.0):.4f} "
                    f"reached hard cap ${skill_hard:.4f}"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )
        if self._session_spent >= self._cfg.session_hard_usd:
            return BudgetCheck(
                status=BudgetStatus.HardBlock,
                message=(
                    f"Turn blocked: session cost ${self._session_spent:.4f} "
                    f"reached hard cap ${self._cfg.session_hard_usd:.4f}"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )
        if spent_day >= self._cfg.day_hard_usd:
            return BudgetCheck(
                status=BudgetStatus.HardBlock,
                message=(
                    f"Turn blocked: today's cost ${spent_day:.4f} "
                    f"reached hard cap ${self._cfg.day_hard_usd:.4f}"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )

        # Soft caps
        if skill_soft is not None and spent_skill.get(task_kind, 0.0) >= skill_soft:
            return BudgetCheck(
                status=BudgetStatus.SoftWarning,
                message=(
                    f"Cost warning: {task_kind} ${spent_skill.get(task_kind, 0.0):.4f} "
                    f"/ ${skill_soft:.4f} soft limit"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )
        if self._session_spent >= self._cfg.session_soft_usd:
            return BudgetCheck(
                status=BudgetStatus.SoftWarning,
                message=(
                    f"Cost warning: session ${self._session_spent:.4f} "
                    f"/ ${self._cfg.session_soft_usd:.4f} soft limit"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )
        if spent_day >= self._cfg.day_soft_usd:
            return BudgetCheck(
                status=BudgetStatus.SoftWarning,
                message=(
                    f"Cost warning: today ${spent_day:.4f} "
                    f"/ ${self._cfg.day_soft_usd:.4f} soft limit"
                ),
                spent_session=self._session_spent,
                spent_day=spent_day,
                spent_skill=spent_skill,
            )

        return BudgetCheck(
            status=BudgetStatus.OK,
            message=None,
            spent_session=self._session_spent,
            spent_day=spent_day,
            spent_skill=spent_skill,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _day_start(self) -> float:
        """Timestamp for the start of the current UTC day."""
        import datetime
        now = datetime.datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start.timestamp()

    def _day_spent(self) -> float:
        """Query DB for total cost since start of today."""
        try:
            from sera.llm.router_stats import cost_since
            return cost_since(self._day_start(), _db=self._db)
        except Exception:
            return 0.0
