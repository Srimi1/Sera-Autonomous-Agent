"""Skill replay verification.

A new skill cannot move out of CANDIDATE state until it passes its
captured replay traces. The verifier runs each `ReplayCase` against the
skill's tool handler, compares output against `expect_substring` /
`expect_equals`, and returns a pass/fail `ReplayResult`. The orchestrator
`verify_via_replay(lifecycle, skill, cases)` flips the lifecycle row to
verified iff *every* case passes — a broken skill stays a candidate.

Outclass: Hermes promotes new skills by lifecycle (age, usage). Sera
promotes by *correctness* — a captured trace must replay cleanly first.
Bad skills can't reach users.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from sera.skills.loader import Skill, skill_to_tool
from sera.skills.lifecycle import SkillLifecycle
from sera.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayCase:
    """One captured input + expected outcome for a skill."""

    id: str
    input: dict[str, Any]
    expect_substring: str = ""
    expect_equals: str = ""


@dataclass(frozen=True)
class ReplayResult:
    case_id: str
    passed: bool
    reason: str = ""
    output: str = ""


@dataclass(frozen=True)
class VerificationReport:
    """Aggregate of all `ReplayResult`s for one skill verification pass."""

    skill_name: str
    results: tuple[ReplayResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def n_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def n_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)


# ─── Replay primitives ───────────────────────────────────────


async def replay_tool(tool: Tool, case: ReplayCase) -> ReplayResult:
    """Invoke `tool.handler(case.input)` and score against expectations.

    Handler exceptions are caught and turned into failing results — a
    misbehaving skill must never crash the verifier pipeline.
    """
    ctx = ToolContext(session_id="replay", workspace="/tmp")
    try:
        output = await tool.handler(dict(case.input), ctx)
    except Exception as e:  # noqa: BLE001 — replay is best-effort
        return ReplayResult(
            case_id=case.id,
            passed=False,
            reason=f"handler raised: {type(e).__name__}: {e}",
            output="",
        )
    output_text = output if isinstance(output, str) else str(output)
    return _score(case, output_text)


async def replay_skill(skill: Skill, case: ReplayCase) -> ReplayResult:
    """High-level wrapper: build the tool from a Skill, run replay."""
    return await replay_tool(skill_to_tool(skill), case)


def _score(case: ReplayCase, output: str) -> ReplayResult:
    if case.expect_equals and output != case.expect_equals:
        return ReplayResult(
            case_id=case.id,
            passed=False,
            reason=f"output != expected {case.expect_equals!r}",
            output=output,
        )
    if case.expect_substring and case.expect_substring not in output:
        return ReplayResult(
            case_id=case.id,
            passed=False,
            reason=f"missing substring {case.expect_substring!r}",
            output=output,
        )
    return ReplayResult(case_id=case.id, passed=True, reason="", output=output)


# ─── Lifecycle-integrated verification ───────────────────────


async def verify_via_replay(
    lifecycle: SkillLifecycle,
    skill: Skill,
    cases: Sequence[ReplayCase],
    *,
    now: float | None = None,
) -> VerificationReport:
    """Run every case against `skill`; flip `verified=True` iff all pass.

    Empty case lists do NOT verify — a skill with no captured trace
    can't be promoted by accident. The lifecycle row is left untouched
    on failure, so a broken skill stays in candidate.
    """
    results: list[ReplayResult] = []
    for case in cases:
        results.append(await replay_skill(skill, case))
    report = VerificationReport(skill_name=skill.name, results=tuple(results))
    if report.passed:
        lifecycle.verify(skill.name, now=now)
    return report


# ─── YAML loader for tests/skill_replay/*.yaml ───────────────


def load_replay_cases(path: Path) -> list[ReplayCase]:
    """Parse a `skill_replay` yaml file into ReplayCase objects.

    Schema:
        skill: <name>
        cases:
          - id: <str>
            input: { ... }       # optional, defaults to {}
            expect:
              substring: <str>   # optional
              equals: <str>      # optional
    """
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    cases_raw: Iterable[dict] = raw.get("cases") or ()
    out: list[ReplayCase] = []
    for c in cases_raw:
        if not isinstance(c, dict):
            continue
        expect = c.get("expect") or {}
        out.append(
            ReplayCase(
                id=str(c.get("id") or "?"),
                input=dict(c.get("input") or {}),
                expect_substring=str(expect.get("substring") or ""),
                expect_equals=str(expect.get("equals") or ""),
            )
        )
    return out


def load_replay_skill_name(path: Path) -> str | None:
    """Read the `skill:` field so the caller knows which skill to verify."""
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    name = raw.get("skill")
    return str(name) if name else None
