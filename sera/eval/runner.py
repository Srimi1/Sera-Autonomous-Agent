"""Per-case runner. Builds an isolated workspace + DB, runs run_turn, scores.

Each case gets:

  * a fresh `tempfile.TemporaryDirectory` workspace seeded with `workspace_files`
  * a fresh `sessions.db` inside the workspace
  * a fresh `Session` via `Session.create`
  * a fresh `StubLLM` (or whatever LLM the caller injected)
  * a fresh `AutoApproveGate(allow=True)` so tool gating doesn't deadlock CI

The runner is deliberately not async-public — `run_cases` wraps the asyncio
boilerplate so callers (CLI + tests) just hand it a list of cases.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from sera.agent.budget import IterationBudget
from sera.agent.loop import TokenSink, run_turn
from sera.eval.cases import EvalCase
from sera.eval.scoring import ScoreVerdict, score
from sera.eval.stub_llm import StubLLM
from sera.eval.telemetry import TelemetryStore, TurnRow
from sera.memory.session import Session
from sera.safety.approval import AutoApproveGate


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    reason: str
    latency_ms: int
    iterations: int
    tool_calls: list[str]
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class RunReport:
    run_id: str
    results: list[CaseResult]

    @property
    def n_pass(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def n_fail(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def passed_all(self) -> bool:
        return self.n_fail == 0


def _silent_sink() -> TokenSink:
    return TokenSink(on_text=lambda _t: None)


def _seed_workspace(workspace: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


async def _run_single(
    case: EvalCase,
    *,
    llm_factory,
    telemetry: TelemetryStore | None,
    run_id: str | None,
) -> CaseResult:
    with tempfile.TemporaryDirectory(prefix="sera-eval-") as wsdir:
        ws = Path(wsdir)
        _seed_workspace(ws, case.workspace_files)
        db = ws / "sessions.db"
        session = Session.create(workspace=str(ws), db_path=db)
        llm = llm_factory(case)

        started = time.perf_counter()
        await run_turn(
            session,
            case.prompt,
            llm,
            sink=_silent_sink(),
            approval=AutoApproveGate(allow=True),
            budget=IterationBudget.of(max(8, len(case.script) + 4)),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        iterations = getattr(llm, "calls", 0)
        verdict: ScoreVerdict = score(case, session, iterations=iterations)
        usage = session.usage_totals()

        tool_calls: list[str] = []
        for m in session.messages:
            if m.role == "assistant":
                for tc in m.tool_calls or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name")
                    if name:
                        tool_calls.append(name)

        if telemetry is not None and run_id is not None:
            telemetry.record(
                run_id,
                TurnRow(
                    case_id=case.id,
                    latency_ms=latency_ms,
                    tool_calls_count=len(tool_calls),
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_tokens", 0),
                    cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                    passed=verdict.passed,
                    reason=verdict.reason,
                ),
            )

        session.close()
        return CaseResult(
            case_id=case.id,
            passed=verdict.passed,
            reason=verdict.reason,
            latency_ms=latency_ms,
            iterations=iterations,
            tool_calls=tool_calls,
            usage=usage,
        )


def _stub_factory(case: EvalCase):
    return StubLLM(list(case.script))


def run_cases(
    cases: list[EvalCase],
    *,
    llm_factory=None,
    telemetry: TelemetryStore | None = None,
    profile: str | None = None,
) -> RunReport:
    """Run every case; record telemetry if a store is provided. Sync API."""
    factory = llm_factory or _stub_factory
    run_id = telemetry.start_run(profile=profile) if telemetry is not None else "ad-hoc"

    async def _go() -> list[CaseResult]:
        out: list[CaseResult] = []
        for case in cases:
            out.append(
                await _run_single(
                    case,
                    llm_factory=factory,
                    telemetry=telemetry,
                    run_id=run_id if telemetry is not None else None,
                )
            )
        return out

    results = asyncio.run(_go())
    if telemetry is not None:
        telemetry.finish_run(run_id)
    return RunReport(run_id=run_id, results=results)
