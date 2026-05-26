"""Tests for sera.llm.router_stats — record, count, p50 aggregation."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.llm.router_stats import _calc_cost, p50_table, record_call, total_calls


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "router_stats.db"


def _rec(db: Path, *, provider="anthropic", model="claude-sonnet-4-6",
         task_kind="chat", latency_ms=500, input_tokens=100, output_tokens=50,
         success=True) -> None:
    record_call(
        provider=provider,
        model=model,
        task_kind=task_kind,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        success=success,
        _db=db,
    )


class TestRecordCall:
    def test_basic_insert(self, db: Path) -> None:
        _rec(db)
        assert total_calls(db) == 1

    def test_multiple_inserts(self, db: Path) -> None:
        _rec(db)
        _rec(db)
        _rec(db)
        assert total_calls(db) == 3

    def test_total_calls_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.db"
        assert total_calls(missing) == 0


class TestCostCalc:
    def test_known_model(self) -> None:
        # claude-sonnet-4-6: $3/1M in, $15/1M out
        cost = _calc_cost("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)
        assert abs(cost - 3.00) < 1e-9

    def test_output_tokens(self) -> None:
        cost = _calc_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000)
        assert abs(cost - 15.00) < 1e-9

    def test_unknown_model_zero_cost(self) -> None:
        cost = _calc_cost("unknown-model-xyz", input_tokens=999, output_tokens=999)
        assert cost == 0.0

    def test_gpt4o_mini(self) -> None:
        cost = _calc_cost("gpt-4o-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert abs(cost - 0.75) < 1e-9  # 0.15 + 0.60


class TestP50Table:
    def test_empty_db(self, db: Path) -> None:
        # touch the file so it exists but is empty
        _rec(db)
        # then wipe and re-check
        db.unlink()
        assert p50_table(db) == []

    def test_single_row(self, db: Path) -> None:
        _rec(db, latency_ms=300)
        rows = p50_table(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["provider"] == "anthropic"
        assert r["model"] == "claude-sonnet-4-6"
        assert r["task_kind"] == "chat"
        assert r["n"] == 1
        assert r["p50_ms"] == 300
        assert r["success_pct"] == pytest.approx(100.0)

    def test_p50_median(self, db: Path) -> None:
        for ms in [100, 200, 300, 400, 500]:
            _rec(db, latency_ms=ms)
        rows = p50_table(db)
        assert len(rows) == 1
        # sorted: [100,200,300,400,500] → index 2 → 300
        assert rows[0]["p50_ms"] == 300

    def test_multiple_task_kinds(self, db: Path) -> None:
        _rec(db, task_kind="chat", latency_ms=200)
        _rec(db, task_kind="tool", latency_ms=800)
        rows = p50_table(db)
        assert len(rows) == 2
        kinds = {r["task_kind"] for r in rows}
        assert kinds == {"chat", "tool"}

    def test_multiple_providers(self, db: Path) -> None:
        _rec(db, provider="anthropic", model="claude-sonnet-4-6")
        _rec(db, provider="openai", model="gpt-4o-mini")
        rows = p50_table(db)
        assert len(rows) == 2
        providers = {r["provider"] for r in rows}
        assert providers == {"anthropic", "openai"}

    def test_success_pct_partial(self, db: Path) -> None:
        _rec(db, success=True)
        _rec(db, success=True)
        _rec(db, success=False)
        rows = p50_table(db)
        assert len(rows) == 1
        assert rows[0]["success_pct"] == pytest.approx(100 * 2 / 3)

    def test_avg_cost(self, db: Path) -> None:
        # 2 calls: 100 input tokens each at claude-sonnet-4-6 ($3/1M in)
        _rec(db, model="claude-sonnet-4-6", input_tokens=100_000, output_tokens=0)
        _rec(db, model="claude-sonnet-4-6", input_tokens=100_000, output_tokens=0)
        rows = p50_table(db)
        expected_per_call = 3.00 * 0.1  # $3/1M × 100k = $0.30
        assert rows[0]["avg_cost_usd"] == pytest.approx(expected_per_call)
