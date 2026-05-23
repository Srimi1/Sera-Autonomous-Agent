"""Tool-gen eval gate — quarantined auto-tools earn promotion by passing eval cases.

Outclass: no auto-tool reaches production without ≥3 passing eval cases.

Flow:
  1. genesis() writes new tool to quarantine_dir (default ~/.sera/tools/quarantine/)
  2. Author provides ToolEvalCase list (≥ min_pass, default 3)
  3. promote_tool() runs run_tool_eval(); on success moves file to auto_dir
  4. Broken tool's file stays in quarantine — never appears as "production"
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from sera.tools.base import Tool, ToolContext
from sera.tools.genesis import DEFAULT_AUTO_DIR, DEFAULT_QUARANTINE_DIR
from sera.tools.registry import get as get_tool


# ---------------------------------------------------------------------------
# Eval case + verdict
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolEvalCase:
    """One canonical input/expectation pair against an auto-tool."""
    name: str
    args: dict
    expect_substring: str | None = None
    expect_regex: str | None = None
    expect_not_error: bool = True
    timeout_s: float = 5.0


@dataclass(frozen=True)
class ToolEvalVerdict:
    case_name: str
    passed: bool
    reason: str = ""
    output: str = ""


@dataclass
class EvalReport:
    tool_name: str
    verdicts: list[ToolEvalVerdict] = field(default_factory=list)

    @property
    def n_pass(self) -> int:
        return sum(1 for v in self.verdicts if v.passed)

    @property
    def n_fail(self) -> int:
        return sum(1 for v in self.verdicts if not v.passed)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def all_passed(self) -> bool:
        return self.n_fail == 0 and self.total > 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _eval_one(tool: Tool, case: ToolEvalCase) -> ToolEvalVerdict:
    ctx = ToolContext(session_id="tool_eval", workspace="/tmp")
    try:
        output = await asyncio.wait_for(tool.handler(case.args, ctx), timeout=case.timeout_s)
    except asyncio.TimeoutError:
        return ToolEvalVerdict(case.name, False, "timeout", "")
    except Exception as exc:  # noqa: BLE001
        if not case.expect_not_error:
            # Errors were expected — count as pass
            return ToolEvalVerdict(case.name, True, "errored as expected", str(exc))
        return ToolEvalVerdict(case.name, False, f"raised {type(exc).__name__}: {exc}", "")

    out_str = str(output)

    if case.expect_substring is not None and case.expect_substring not in out_str:
        return ToolEvalVerdict(
            case.name, False,
            f"expected substring {case.expect_substring!r} not in output",
            out_str,
        )
    if case.expect_regex is not None and not re.search(case.expect_regex, out_str):
        return ToolEvalVerdict(
            case.name, False,
            f"output did not match regex {case.expect_regex!r}",
            out_str,
        )

    return ToolEvalVerdict(case.name, True, "ok", out_str)


async def run_tool_eval(tool: Tool, cases: list[ToolEvalCase]) -> EvalReport:
    """Run every case against `tool`. Returns aggregate report."""
    report = EvalReport(tool_name=tool.name)
    for case in cases:
        report.verdicts.append(await _eval_one(tool, case))
    return report


# ---------------------------------------------------------------------------
# Promotion — moves quarantined file to auto/ on enough passes
# ---------------------------------------------------------------------------

@dataclass
class PromotionResult:
    ok: bool
    tool_name: str
    n_pass: int = 0
    n_fail: int = 0
    promoted_to: Path | None = None
    reason: str = ""
    report: EvalReport | None = None

    def summary(self) -> str:
        if self.ok:
            return f"promoted {self.tool_name}: {self.n_pass}/{self.n_pass + self.n_fail} passed"
        return f"quarantined {self.tool_name}: {self.reason}"


async def promote_tool(
    tool_name: str,
    cases: list[ToolEvalCase],
    *,
    quarantine_dir: Path | None = None,
    auto_dir: Path | None = None,
    min_pass: int = 3,
) -> PromotionResult:
    """Run eval against the registered tool; promote on ≥ min_pass passing cases.

    Args:
        tool_name:        Name of the registered tool to evaluate.
        cases:            Eval cases to run. Caller MUST provide ≥ min_pass.
        quarantine_dir:   Where the auto-tool file currently lives.
        auto_dir:         Promotion target.
        min_pass:         Minimum number of passing cases required (default 3).

    Returns:
        PromotionResult. On failure, the quarantined file is NOT moved.
    """
    quarantine_dir = quarantine_dir or DEFAULT_QUARANTINE_DIR
    auto_dir = auto_dir or DEFAULT_AUTO_DIR

    tool = get_tool(tool_name)
    if tool is None:
        return PromotionResult(False, tool_name, reason=f"tool {tool_name!r} not in registry")

    if len(cases) < min_pass:
        return PromotionResult(
            False, tool_name,
            n_fail=len(cases),
            reason=f"need at least {min_pass} eval cases, got {len(cases)}",
        )

    report = await run_tool_eval(tool, cases)
    if report.n_pass < min_pass:
        return PromotionResult(
            False, tool_name,
            n_pass=report.n_pass,
            n_fail=report.n_fail,
            reason=f"only {report.n_pass}/{report.total} cases passed (need {min_pass})",
            report=report,
        )

    src = quarantine_dir / f"{tool_name}.py"
    if not src.exists():
        return PromotionResult(
            False, tool_name,
            n_pass=report.n_pass,
            n_fail=report.n_fail,
            reason=f"quarantined file not found at {src}",
            report=report,
        )

    auto_dir.mkdir(parents=True, exist_ok=True)
    dst = auto_dir / f"{tool_name}.py"
    src.replace(dst)
    return PromotionResult(
        True, tool_name,
        n_pass=report.n_pass,
        n_fail=report.n_fail,
        promoted_to=dst,
        reason="promoted",
        report=report,
    )


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------

def list_quarantined(quarantine_dir: Path | None = None) -> list[Path]:
    d = quarantine_dir or DEFAULT_QUARANTINE_DIR
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.py") if p.is_file())


def is_quarantined(tool_name: str, quarantine_dir: Path | None = None) -> bool:
    d = quarantine_dir or DEFAULT_QUARANTINE_DIR
    return (d / f"{tool_name}.py").exists()


def is_promoted(tool_name: str, auto_dir: Path | None = None) -> bool:
    d = auto_dir or DEFAULT_AUTO_DIR
    return (d / f"{tool_name}.py").exists()
