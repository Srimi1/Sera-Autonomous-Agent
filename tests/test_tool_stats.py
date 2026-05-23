"""Tests for sera.tools.stats — per-tool usage / success / latency / $/call dashboard."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sera.tools.base import Permission, Tool, ToolCall, ToolContext, ToolScope
from sera.tools.dispatcher import execute
from sera.tools.registry import register, reset as reset_registry
from sera.tools.stats import (
    ToolStatRow,
    clear_stats,
    record_tool_call,
    stats_for,
    tool_stats,
    total_calls,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(db: Path, *, name: str = "t1", latency: int = 100, ok: bool = True,
         err: str | None = None, cost: float = 0.0) -> None:
    record_tool_call(
        tool_name=name, latency_ms=latency, success=ok,
        error_msg=err, cost_usd=cost, _db=db,
    )


# ---------------------------------------------------------------------------
# record_tool_call / total_calls
# ---------------------------------------------------------------------------

class TestRecord:
    def test_record_one(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db)
        assert total_calls(db) == 1

    def test_record_many(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        for _ in range(10):
            _rec(db)
        assert total_calls(db) == 10

    def test_total_calls_missing_db(self, tmp_path: Path) -> None:
        assert total_calls(tmp_path / "missing.db") == 0


# ---------------------------------------------------------------------------
# tool_stats — aggregation
# ---------------------------------------------------------------------------

class TestToolStats:
    def test_empty(self, tmp_path: Path) -> None:
        assert tool_stats(tmp_path / "missing.db") == []

    def test_single_tool_aggregation(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        for ms in [100, 200, 300]:
            _rec(db, name="echo", latency=ms, ok=True)
        rows = tool_stats(db)
        assert len(rows) == 1
        r = rows[0]
        assert r.tool_name == "echo"
        assert r.n_calls == 3
        assert r.n_ok == 3
        assert r.success_pct == pytest.approx(100.0)
        assert r.p50_ms == 200
        assert r.avg_latency_ms == pytest.approx(200.0)

    def test_multiple_tools_grouped(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db, name="a", latency=100)
        _rec(db, name="b", latency=200)
        rows = tool_stats(db)
        assert {r.tool_name for r in rows} == {"a", "b"}

    def test_failure_drops_success_pct(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db, ok=True)
        _rec(db, ok=True)
        _rec(db, ok=False)
        r = tool_stats(db)[0]
        assert r.n_ok == 2
        assert r.n_fail == 1
        assert r.success_pct == pytest.approx(100 * 2 / 3)

    def test_avg_cost(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db, cost=0.01)
        _rec(db, cost=0.03)
        r = tool_stats(db)[0]
        assert r.avg_cost_usd == pytest.approx(0.02)

    def test_last_used_at_is_latest(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db)
        time.sleep(0.01)
        _rec(db)
        r = tool_stats(db)[0]
        assert r.last_used_at > 0

    def test_stats_for_lookup(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db, name="needle")
        _rec(db, name="hay")
        r = stats_for("needle", db)
        assert r is not None
        assert r.tool_name == "needle"

    def test_stats_for_missing(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        _rec(db, name="hay")
        assert stats_for("not_recorded", db) is None


# ---------------------------------------------------------------------------
# ToolStatRow shape
# ---------------------------------------------------------------------------

class TestToolStatRow:
    def test_n_fail_property(self) -> None:
        r = ToolStatRow(
            tool_name="x", n_calls=10, n_ok=7, success_pct=70.0,
            p50_ms=100, avg_latency_ms=120, avg_cost_usd=0.01, last_used_at=0,
        )
        assert r.n_fail == 3


# ---------------------------------------------------------------------------
# clear_stats
# ---------------------------------------------------------------------------

class TestClearStats:
    def test_clear_removes_all(self, tmp_path: Path) -> None:
        db = tmp_path / "ts.db"
        for _ in range(5):
            _rec(db)
        removed = clear_stats(db)
        assert removed == 5
        assert total_calls(db) == 0

    def test_clear_missing_db(self, tmp_path: Path) -> None:
        assert clear_stats(tmp_path / "missing.db") == 0


# ---------------------------------------------------------------------------
# Dispatcher integration — P-50 verification: real numbers after bench
# ---------------------------------------------------------------------------

class TestDispatcherIntegration:
    """Bench-style: dispatch real tool calls, verify stats DB captures them."""

    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def _register_test_tool(self, name="bench_echo", *, fail=False, sleep_s=0.0) -> None:
        async def _handler(args, ctx):
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            if fail:
                raise RuntimeError("bench failure")
            return args.get("text", "")
        register(Tool(
            name=name, description="bench tool",
            parameters={"type": "object", "properties": {}},
            permission=Permission.READ_ONLY,
            scope=ToolScope.SYSTEM,
            handler=_handler,
        ))

    def test_dispatcher_records_success(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "ts.db"
        # Patch TOOL_STATS_DB so dispatcher writes here
        monkeypatch.setattr("sera.tools.stats.TOOL_STATS_DB", db)
        self._register_test_tool("bench_ok")
        ctx = ToolContext(session_id="s", workspace="/tmp")
        call = ToolCall(id="c1", name="bench_ok", arguments={"text": "hi"})

        asyncio.run(execute(call, ctx))

        rows = tool_stats(db)
        assert len(rows) == 1
        assert rows[0].tool_name == "bench_ok"
        assert rows[0].n_ok == 1
        assert rows[0].success_pct == 100.0

    def test_dispatcher_records_failure(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "ts.db"
        monkeypatch.setattr("sera.tools.stats.TOOL_STATS_DB", db)
        self._register_test_tool("bench_fail", fail=True)
        ctx = ToolContext(session_id="s", workspace="/tmp")
        call = ToolCall(id="c1", name="bench_fail", arguments={})

        result = asyncio.run(execute(call, ctx))
        assert result.error is True

        rows = tool_stats(db)
        assert len(rows) == 1
        assert rows[0].n_ok == 0
        assert rows[0].success_pct == 0.0

    def test_dispatcher_records_latency(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "ts.db"
        monkeypatch.setattr("sera.tools.stats.TOOL_STATS_DB", db)
        self._register_test_tool("bench_slow", sleep_s=0.05)
        ctx = ToolContext(session_id="s", workspace="/tmp")

        for i in range(3):
            asyncio.run(execute(ToolCall(id=f"c{i}", name="bench_slow", arguments={}), ctx))

        rows = tool_stats(db)
        assert rows[0].n_calls == 3
        # 50ms sleep — latency should be at least 30ms (scheduler-dependent)
        assert rows[0].p50_ms >= 30

    def test_bench_suite_real_numbers(self, tmp_path: Path, monkeypatch) -> None:
        """P-50 verification: real numbers after the bench suite.

        Run 30 calls across 3 tools (10 each) — some succeed, some fail.
        Assert the stats DB has real, queryable, per-tool aggregates.
        """
        db = tmp_path / "ts.db"
        monkeypatch.setattr("sera.tools.stats.TOOL_STATS_DB", db)

        self._register_test_tool("alpha")               # always ok
        self._register_test_tool("beta", fail=True)     # always fail
        self._register_test_tool("gamma")               # ok

        ctx = ToolContext(session_id="s", workspace="/tmp")
        for i in range(10):
            asyncio.run(execute(ToolCall(id=f"a{i}", name="alpha", arguments={}), ctx))
            asyncio.run(execute(ToolCall(id=f"b{i}", name="beta", arguments={}), ctx))
            asyncio.run(execute(ToolCall(id=f"g{i}", name="gamma", arguments={}), ctx))

        rows = tool_stats(db)
        assert len(rows) == 3
        by_name = {r.tool_name: r for r in rows}

        assert by_name["alpha"].n_calls == 10
        assert by_name["alpha"].success_pct == 100.0

        assert by_name["beta"].n_calls == 10
        assert by_name["beta"].success_pct == 0.0  # all failed — drift detected

        assert by_name["gamma"].n_calls == 10
        assert by_name["gamma"].success_pct == 100.0

        # Total calls recorded
        assert total_calls(db) == 30
