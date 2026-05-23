"""Tests for sera.eval.tool_eval — auto-tool quarantine + promotion gate."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.eval.tool_eval import (
    EvalReport,
    PromotionResult,
    ToolEvalCase,
    ToolEvalVerdict,
    is_promoted,
    is_quarantined,
    list_quarantined,
    promote_tool,
    run_tool_eval,
)
from sera.tools.base import Permission, Tool, ToolScope
from sera.tools.genesis import ToolSpec, genesis
from sera.tools.registry import all_tools, reset as reset_registry


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers — direct Tool construction (skip genesis pipeline for unit tests)
# ---------------------------------------------------------------------------

def _echo_tool(name: str = "echo") -> Tool:
    async def _handler(args, ctx):
        return f"echo: {args.get('text', '')}"
    return Tool(
        name=name, description="echo", parameters={"type": "object", "properties": {}},
        permission=Permission.READ_ONLY, scope=ToolScope.SYSTEM, handler=_handler,
    )


def _broken_tool(name: str = "broken") -> Tool:
    async def _handler(args, ctx):
        raise RuntimeError("kaboom")
    return Tool(
        name=name, description="broken", parameters={"type": "object", "properties": {}},
        permission=Permission.READ_ONLY, scope=ToolScope.SYSTEM, handler=_handler,
    )


def _hang_tool(name: str = "hang") -> Tool:
    async def _handler(args, ctx):
        await asyncio.sleep(10)
        return "never"
    return Tool(
        name=name, description="hang", parameters={"type": "object", "properties": {}},
        permission=Permission.READ_ONLY, scope=ToolScope.SYSTEM, handler=_handler,
    )


# ---------------------------------------------------------------------------
# ToolEvalCase shape
# ---------------------------------------------------------------------------

class TestToolEvalCase:
    def test_defaults(self) -> None:
        case = ToolEvalCase(name="c", args={})
        assert case.timeout_s == 5.0
        assert case.expect_not_error is True

    def test_with_expectations(self) -> None:
        case = ToolEvalCase(name="c", args={"x": 1}, expect_substring="hello")
        assert case.expect_substring == "hello"


# ---------------------------------------------------------------------------
# run_tool_eval — basic flows
# ---------------------------------------------------------------------------

class TestRunToolEval:
    def test_passing_tool_all_pass(self) -> None:
        tool = _echo_tool()
        cases = [
            ToolEvalCase("c1", {"text": "hello"}, expect_substring="hello"),
            ToolEvalCase("c2", {"text": "world"}, expect_substring="world"),
            ToolEvalCase("c3", {"text": ""}, expect_substring="echo"),
        ]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 3
        assert report.n_fail == 0
        assert report.all_passed

    def test_substring_miss(self) -> None:
        tool = _echo_tool()
        cases = [ToolEvalCase("c1", {"text": "abc"}, expect_substring="xyz")]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 0
        assert report.n_fail == 1
        assert "substring" in report.verdicts[0].reason

    def test_regex_match(self) -> None:
        tool = _echo_tool()
        cases = [ToolEvalCase("c1", {"text": "abc123"}, expect_regex=r"\d+")]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 1

    def test_regex_miss(self) -> None:
        tool = _echo_tool()
        cases = [ToolEvalCase("c1", {"text": "abc"}, expect_regex=r"\d+")]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 0
        assert "regex" in report.verdicts[0].reason

    def test_broken_tool_all_fail(self) -> None:
        tool = _broken_tool()
        cases = [ToolEvalCase(f"c{i}", {}) for i in range(3)]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 0
        assert report.n_fail == 3
        assert all("kaboom" in v.reason or "RuntimeError" in v.reason for v in report.verdicts)

    def test_timeout_counts_as_fail(self) -> None:
        tool = _hang_tool()
        cases = [ToolEvalCase("c1", {}, timeout_s=0.1)]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 0
        assert "timeout" in report.verdicts[0].reason

    def test_expect_not_error_false_inverts(self) -> None:
        """A tool that's supposed to raise — raising counts as pass."""
        tool = _broken_tool()
        cases = [ToolEvalCase("c1", {}, expect_not_error=False)]
        report = _run(run_tool_eval(tool, cases))
        assert report.n_pass == 1


# ---------------------------------------------------------------------------
# EvalReport
# ---------------------------------------------------------------------------

class TestEvalReport:
    def test_empty_not_all_passed(self) -> None:
        r = EvalReport(tool_name="x")
        assert not r.all_passed
        assert r.total == 0

    def test_partial_pass(self) -> None:
        r = EvalReport(tool_name="x", verdicts=[
            ToolEvalVerdict("a", True), ToolEvalVerdict("b", False), ToolEvalVerdict("c", True),
        ])
        assert r.n_pass == 2
        assert r.n_fail == 1
        assert not r.all_passed


# ---------------------------------------------------------------------------
# promote_tool — minimum-pass gate
# ---------------------------------------------------------------------------

class TestPromotionGate:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def _write_quarantined(self, quarantine_dir: Path, name: str) -> None:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        (quarantine_dir / f"{name}.py").write_text(f"# stub for {name}\n")

    def _passing_cases(self) -> list[ToolEvalCase]:
        return [
            ToolEvalCase("c1", {"text": "alpha"}, expect_substring="alpha"),
            ToolEvalCase("c2", {"text": "beta"}, expect_substring="beta"),
            ToolEvalCase("c3", {"text": "gamma"}, expect_substring="gamma"),
        ]

    def test_3_passing_cases_promotes(self, tmp_path: Path) -> None:
        from sera.tools.registry import register
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"
        self._write_quarantined(quarantine, "echo")
        register(_echo_tool("echo"))

        result = _run(promote_tool(
            "echo", self._passing_cases(),
            quarantine_dir=quarantine, auto_dir=auto,
        ))

        assert result.ok, result.reason
        assert result.n_pass == 3
        assert result.promoted_to is not None
        assert (auto / "echo.py").exists()
        assert not (quarantine / "echo.py").exists()

    def test_under_min_pass_quarantined(self, tmp_path: Path) -> None:
        """Verification: broken auto-tool stays quarantined."""
        from sera.tools.registry import register
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"
        self._write_quarantined(quarantine, "broken_tool")
        register(_broken_tool("broken_tool"))

        cases = [ToolEvalCase(f"c{i}", {}) for i in range(3)]
        result = _run(promote_tool(
            "broken_tool", cases,
            quarantine_dir=quarantine, auto_dir=auto,
        ))

        assert not result.ok
        assert result.n_pass == 0
        assert result.n_fail == 3
        # File stays in quarantine, NOT promoted
        assert (quarantine / "broken_tool.py").exists()
        assert not (auto / "broken_tool.py").exists()
        assert "passed" in result.reason

    def test_partial_pass_below_threshold_quarantined(self, tmp_path: Path) -> None:
        """2/3 passes is not enough — stays quarantined."""
        from sera.tools.registry import register
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"
        self._write_quarantined(quarantine, "partial")
        register(_echo_tool("partial"))

        cases = [
            ToolEvalCase("c1", {"text": "alpha"}, expect_substring="alpha"),
            ToolEvalCase("c2", {"text": "beta"},  expect_substring="beta"),
            ToolEvalCase("c3", {"text": "abc"},   expect_substring="MUST_NOT_MATCH"),  # fail
        ]
        result = _run(promote_tool(
            "partial", cases,
            quarantine_dir=quarantine, auto_dir=auto, min_pass=3,
        ))
        assert not result.ok
        assert result.n_pass == 2
        assert (quarantine / "partial.py").exists()
        assert not (auto / "partial.py").exists()

    def test_fewer_than_min_cases_rejected(self, tmp_path: Path) -> None:
        from sera.tools.registry import register
        register(_echo_tool("not_enough_cases"))
        result = _run(promote_tool(
            "not_enough_cases",
            [ToolEvalCase("c1", {"text": "x"}, expect_substring="x")],  # only 1 case
            quarantine_dir=tmp_path, auto_dir=tmp_path,
        ))
        assert not result.ok
        assert "at least" in result.reason or "got" in result.reason

    def test_unknown_tool_rejected(self, tmp_path: Path) -> None:
        result = _run(promote_tool(
            "nonexistent_tool", self._passing_cases(),
            quarantine_dir=tmp_path, auto_dir=tmp_path,
        ))
        assert not result.ok
        assert "not in registry" in result.reason

    def test_missing_quarantine_file_blocks_promotion(self, tmp_path: Path) -> None:
        from sera.tools.registry import register
        register(_echo_tool("orphan"))
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"
        quarantine.mkdir()  # empty, no file for "orphan"

        result = _run(promote_tool(
            "orphan", self._passing_cases(),
            quarantine_dir=quarantine, auto_dir=auto,
        ))
        assert not result.ok
        assert "not found" in result.reason


# ---------------------------------------------------------------------------
# Quarantine inspection
# ---------------------------------------------------------------------------

class TestQuarantineInspection:
    def test_list_quarantined(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("# a")
        (tmp_path / "b.py").write_text("# b")
        files = list_quarantined(tmp_path)
        assert len(files) == 2
        assert {f.stem for f in files} == {"a", "b"}

    def test_list_quarantined_empty(self, tmp_path: Path) -> None:
        files = list_quarantined(tmp_path)
        assert files == []

    def test_is_quarantined_true(self, tmp_path: Path) -> None:
        (tmp_path / "x.py").write_text("")
        assert is_quarantined("x", tmp_path) is True

    def test_is_quarantined_false(self, tmp_path: Path) -> None:
        assert is_quarantined("missing", tmp_path) is False

    def test_is_promoted_true(self, tmp_path: Path) -> None:
        (tmp_path / "y.py").write_text("")
        assert is_promoted("y", tmp_path) is True


# ---------------------------------------------------------------------------
# Full flow: genesis → quarantine → eval → promote
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_genesis_then_promote(self, tmp_path: Path) -> None:
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"

        spec = ToolSpec(
            name="echo_promo",
            description="Echo tool for promotion test.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            handler_body="return f\"echo: {args.get('text', '')}\"",
            permission="READ_ONLY",
        )
        # Step 1: genesis writes to quarantine
        gen = _run(genesis(spec, auto_dir=quarantine, skip_mypy=True))
        assert gen.ok
        assert (quarantine / "echo_promo.py").exists()

        # Step 2: promote with 3 passing cases
        cases = [
            ToolEvalCase("c1", {"text": "alpha"}, expect_substring="alpha"),
            ToolEvalCase("c2", {"text": "beta"},  expect_substring="beta"),
            ToolEvalCase("c3", {"text": "gamma"}, expect_substring="gamma"),
        ]
        promo = _run(promote_tool(
            "echo_promo", cases,
            quarantine_dir=quarantine, auto_dir=auto,
        ))
        assert promo.ok, promo.reason
        assert (auto / "echo_promo.py").exists()
        assert not (quarantine / "echo_promo.py").exists()

    def test_broken_genesis_stays_quarantined(self, tmp_path: Path) -> None:
        """End-to-end: a tool that fails its eval stays in quarantine."""
        quarantine = tmp_path / "quarantine"
        auto = tmp_path / "auto"

        # A tool whose handler always returns "always wrong" — eval expects "right"
        spec = ToolSpec(
            name="wrong_tool",
            description="Always returns wrong output.",
            parameters={"type": "object", "properties": {}},
            handler_body="return 'always wrong'",
            permission="READ_ONLY",
        )
        gen = _run(genesis(spec, auto_dir=quarantine, skip_mypy=True))
        assert gen.ok
        assert (quarantine / "wrong_tool.py").exists()

        cases = [ToolEvalCase(f"c{i}", {}, expect_substring="right") for i in range(3)]
        promo = _run(promote_tool(
            "wrong_tool", cases,
            quarantine_dir=quarantine, auto_dir=auto,
        ))
        # All 3 cases fail → no promotion → file stays in quarantine
        assert not promo.ok
        assert promo.n_pass == 0
        assert (quarantine / "wrong_tool.py").exists()
        assert not (auto / "wrong_tool.py").exists()
