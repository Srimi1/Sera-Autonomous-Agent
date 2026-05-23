"""Tests for sera.llm.budget — cost ceilings, soft warnings, hard blocks."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.llm.budget import (
    BudgetCheck,
    BudgetConfig,
    BudgetEnforcer,
    BudgetExceeded,
    BudgetStatus,
)


# ---------------------------------------------------------------------------
# BudgetConfig
# ---------------------------------------------------------------------------

class TestBudgetConfig:
    def test_defaults(self) -> None:
        cfg = BudgetConfig()
        assert cfg.session_soft_usd == 0.50
        assert cfg.session_hard_usd == 1.00
        assert cfg.day_soft_usd == 2.00
        assert cfg.day_hard_usd == 5.00
        assert cfg.skill_limits == {}

    def test_from_config_empty(self) -> None:
        cfg = BudgetConfig.from_config({})
        assert cfg.session_soft_usd == 0.50

    def test_from_config_overrides(self) -> None:
        cfg = BudgetConfig.from_config({
            "budget": {
                "session_soft_usd": 0.10,
                "session_hard_usd": 0.20,
                "day_soft_usd": 1.00,
                "day_hard_usd": 2.00,
            }
        })
        assert cfg.session_soft_usd == pytest.approx(0.10)
        assert cfg.session_hard_usd == pytest.approx(0.20)

    def test_from_config_skill_limits(self) -> None:
        cfg = BudgetConfig.from_config({
            "budget": {"skill_limits": {"tool": [0.05, 0.10]}}
        })
        assert cfg.skill_limits["tool"] == (0.05, 0.10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enforcer(
    *,
    session_soft: float = 10.0,
    session_hard: float = 20.0,
    day_soft: float = 50.0,
    day_hard: float = 100.0,
    skill_limits: dict | None = None,
) -> BudgetEnforcer:
    cfg = BudgetConfig(
        session_soft_usd=session_soft,
        session_hard_usd=session_hard,
        day_soft_usd=day_soft,
        day_hard_usd=day_hard,
        skill_limits=skill_limits or {},
    )
    # Use nonexistent DB path so day_spent always returns 0.0 in tests
    return BudgetEnforcer(cfg, _db=Path("/nonexistent/db.db"))


# ---------------------------------------------------------------------------
# BudgetEnforcer — OK path
# ---------------------------------------------------------------------------

class TestEnforcerOK:
    def test_fresh_enforcer_ok(self) -> None:
        e = _enforcer()
        result = e.check()
        assert result.status == BudgetStatus.OK
        assert result.message is None
        assert result.ok

    def test_small_spend_ok(self) -> None:
        e = _enforcer(session_soft=1.0, session_hard=2.0)
        e.add(0.10)
        result = e.check()
        assert result.ok
        assert result.spent_session == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# BudgetEnforcer — soft warning (P-39 verification: soft cap triggers banner)
# ---------------------------------------------------------------------------

class TestEnforcerSoftWarning:
    def test_session_soft_triggers(self) -> None:
        e = _enforcer(session_soft=0.10, session_hard=1.0)
        e.add(0.10)  # exactly at soft cap
        result = e.check()
        assert result.status == BudgetStatus.SoftWarning
        assert result.warning
        assert "session" in result.message.lower()
        assert "0.1000" in result.message

    def test_below_soft_no_warning(self) -> None:
        e = _enforcer(session_soft=0.50, session_hard=1.0)
        e.add(0.49)
        assert e.check().ok

    def test_skill_soft_triggers(self) -> None:
        e = _enforcer(skill_limits={"tool": (0.05, 0.50)})
        e.add(0.05, task_kind="tool")
        result = e.check(task_kind="tool")
        assert result.status == BudgetStatus.SoftWarning
        assert "tool" in result.message

    def test_skill_soft_other_kind_unaffected(self) -> None:
        e = _enforcer(skill_limits={"tool": (0.05, 0.50)})
        e.add(0.05, task_kind="tool")
        result = e.check(task_kind="chat")  # chat has no skill limit
        assert result.ok


# ---------------------------------------------------------------------------
# BudgetEnforcer — hard block (P-39 verification: hard cap refuses turn)
# ---------------------------------------------------------------------------

class TestEnforcerHardBlock:
    def test_session_hard_blocks(self) -> None:
        e = _enforcer(session_soft=0.05, session_hard=0.10)
        e.add(0.10)  # exactly at hard cap
        result = e.check()
        assert result.status == BudgetStatus.HardBlock
        assert result.blocked
        assert "session" in result.message.lower()

    def test_session_hard_message_contains_cap(self) -> None:
        e = _enforcer(session_soft=0.05, session_hard=0.10)
        e.add(0.15)
        result = e.check()
        assert "0.1000" in result.message

    def test_multiple_adds_accumulate(self) -> None:
        e = _enforcer(session_soft=0.50, session_hard=1.00)
        for _ in range(10):
            e.add(0.11)  # 1.10 total → over hard cap
        result = e.check()
        assert result.blocked

    def test_skill_hard_blocks(self) -> None:
        e = _enforcer(skill_limits={"tool": (0.05, 0.10)})
        e.add(0.10, task_kind="tool")
        result = e.check(task_kind="tool")
        assert result.blocked
        assert "tool" in result.message

    def test_hard_takes_priority_over_soft(self) -> None:
        e = _enforcer(session_soft=0.05, session_hard=0.10)
        e.add(0.15)  # past both
        assert e.check().status == BudgetStatus.HardBlock


# ---------------------------------------------------------------------------
# BudgetExceeded exception pattern
# ---------------------------------------------------------------------------

class TestBudgetExceededException:
    def test_raise_on_blocked(self) -> None:
        e = _enforcer(session_soft=0.01, session_hard=0.02)
        e.add(0.05)
        check = e.check()
        assert check.blocked
        with pytest.raises(BudgetExceeded):
            if check.blocked:
                raise BudgetExceeded(check.message)


# ---------------------------------------------------------------------------
# BudgetEnforcer — reset_session
# ---------------------------------------------------------------------------

class TestResetSession:
    def test_reset_clears_session_spend(self) -> None:
        e = _enforcer(session_soft=0.10, session_hard=0.20)
        e.add(0.15)
        assert e.check().status == BudgetStatus.SoftWarning
        e.reset_session()
        assert e.check().ok

    def test_reset_clears_skill_spend(self) -> None:
        e = _enforcer(skill_limits={"tool": (0.05, 0.10)})
        e.add(0.08, task_kind="tool")
        e.reset_session()
        assert e.check(task_kind="tool").ok


# ---------------------------------------------------------------------------
# BudgetCheck properties
# ---------------------------------------------------------------------------

class TestBudgetCheck:
    def test_ok_properties(self) -> None:
        check = BudgetCheck(
            status=BudgetStatus.OK, message=None,
            spent_session=0.0, spent_day=0.0, spent_skill={}
        )
        assert check.ok and not check.blocked and not check.warning

    def test_soft_properties(self) -> None:
        check = BudgetCheck(
            status=BudgetStatus.SoftWarning, message="warn",
            spent_session=0.5, spent_day=0.0, spent_skill={}
        )
        assert not check.ok and not check.blocked and check.warning

    def test_hard_properties(self) -> None:
        check = BudgetCheck(
            status=BudgetStatus.HardBlock, message="block",
            spent_session=1.0, spent_day=0.0, spent_skill={}
        )
        assert not check.ok and check.blocked and not check.warning


# ---------------------------------------------------------------------------
# cost_since (router_stats) — new helper from P-39
# ---------------------------------------------------------------------------

class TestCostSince:
    def test_cost_since_missing_db(self, tmp_path: Path) -> None:
        from sera.llm.router_stats import cost_since
        assert cost_since(0.0, _db=tmp_path / "missing.db") == 0.0

    def test_cost_since_sums_recent(self, tmp_path: Path) -> None:
        import time
        from sera.llm.router_stats import cost_since, record_call

        db = tmp_path / "rs.db"
        t0 = time.time() - 1
        record_call(
            provider="anthropic", model="claude-sonnet-4-6",
            task_kind="chat", latency_ms=500,
            input_tokens=1_000_000, output_tokens=0,
            success=True, _db=db,
        )
        assert cost_since(t0, _db=db) == pytest.approx(3.00)

    def test_cost_since_filters_old(self, tmp_path: Path) -> None:
        import time
        from sera.llm.router_stats import cost_since, record_call

        db = tmp_path / "rs.db"
        record_call(
            provider="anthropic", model="claude-sonnet-4-6",
            task_kind="chat", latency_ms=500,
            input_tokens=1_000_000, output_tokens=0,
            success=True, _db=db,
        )
        assert cost_since(time.time() + 3600, _db=db) == 0.0

    def test_cost_since_filters_task_kind(self, tmp_path: Path) -> None:
        import time
        from sera.llm.router_stats import cost_since, record_call

        db = tmp_path / "rs.db"
        t0 = time.time() - 1
        record_call(
            provider="anthropic", model="claude-sonnet-4-6",
            task_kind="chat", latency_ms=200,
            input_tokens=1_000_000, output_tokens=0,
            success=True, _db=db,
        )
        record_call(
            provider="openai", model="gpt-4o-mini",
            task_kind="tool", latency_ms=100,
            input_tokens=1_000_000, output_tokens=0,
            success=True, _db=db,
        )
        assert cost_since(t0, task_kind="chat", _db=db) == pytest.approx(3.00)
        assert cost_since(t0, task_kind="tool", _db=db) == pytest.approx(0.15)
