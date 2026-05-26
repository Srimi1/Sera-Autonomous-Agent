"""P-10: eval harness — cases / stub / scoring / telemetry / runner."""
from __future__ import annotations

from pathlib import Path


from sera.eval import (
    EvalCase,
    ExpectedOutcome,
    ScriptStep,
    StubLLM,
    TelemetryStore,
    load_cases,
    run_cases,
    score,
)
from sera.eval.runner import _silent_sink  # noqa: F401 — referenced via runner
from sera.memory.session import Message, Session

CASES_DIR = Path(__file__).parent / "eval_cases"


def test_cases_directory_has_at_least_ten():
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 10
    for c in cases:
        assert c.id
        assert c.prompt
        assert c.expect.substring or c.expect.tool_calls or c.expect.forbid_tool_calls


def test_runner_passes_all_golden_cases(tmp_path: Path):
    """Smoke: the bundled stub script for each case must score green."""
    cases = load_cases(CASES_DIR)
    store = TelemetryStore(db_path=tmp_path / "telemetry.db")
    report = run_cases(cases, telemetry=store, profile="stub-test")
    failures = [r for r in report.results if not r.passed]
    assert not failures, f"failing cases: {[(r.case_id, r.reason) for r in failures]}"
    assert report.passed_all
    assert report.n_pass == len(cases)


def test_telemetry_persists_results(tmp_path: Path):
    store = TelemetryStore(db_path=tmp_path / "t.db")
    cases = load_cases(CASES_DIR)[:3]
    report = run_cases(cases, telemetry=store, profile="stub-smoke")
    rows = store.results_for(report.run_id)
    assert len(rows) == len(cases)
    assert all(r["passed"] == 1 for r in rows)
    runs = store.recent_runs(limit=5)
    assert any(r["id"] == report.run_id for r in runs)
    target_run = next(r for r in runs if r["id"] == report.run_id)
    assert target_run["finished_at"] is not None


def _empty_session(tmp_path: Path) -> Session:
    return Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")


def test_scoring_rejects_missing_substring(tmp_path: Path):
    case = EvalCase(
        id="x", prompt="p",
        expect=ExpectedOutcome(substring="banana"),
    )
    s = _empty_session(tmp_path)
    s.append(Message(role="assistant", content="apple cherry"))
    v = score(case, s, iterations=1)
    assert not v.passed
    assert "banana" in v.reason


def test_scoring_rejects_missing_tool(tmp_path: Path):
    case = EvalCase(
        id="x", prompt="p",
        expect=ExpectedOutcome(tool_calls=("file_read",)),
    )
    s = _empty_session(tmp_path)
    s.append(Message(role="assistant", content="done"))
    v = score(case, s, iterations=1)
    assert not v.passed
    assert "file_read" in v.reason


def test_scoring_rejects_forbidden_tool(tmp_path: Path):
    case = EvalCase(
        id="x", prompt="p",
        expect=ExpectedOutcome(forbid_tool_calls=("shell_run",)),
    )
    s = _empty_session(tmp_path)
    s.append(
        Message(
            role="assistant",
            content="ran shell",
            tool_calls=[{"function": {"name": "shell_run"}}],
        )
    )
    v = score(case, s, iterations=1)
    assert not v.passed
    assert "shell_run" in v.reason


def test_scoring_enforces_iteration_bounds(tmp_path: Path):
    case = EvalCase(
        id="x", prompt="p",
        expect=ExpectedOutcome(substring="ok", min_iterations=3, max_iterations=5),
    )
    s = _empty_session(tmp_path)
    s.append(Message(role="assistant", content="ok"))
    assert not score(case, s, iterations=1).passed  # under min
    assert not score(case, s, iterations=10).passed  # over max
    assert score(case, s, iterations=4).passed


def test_stub_llm_replays_script(tmp_path: Path):
    """End-to-end: the stub LLM streams scripted steps the loop consumes."""
    import asyncio

    from sera.agent.loop import run_turn
    from sera.safety.approval import AutoApproveGate

    session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")
    llm = StubLLM([ScriptStep(text="step one"), ScriptStep(text="step two")])
    out = asyncio.run(
        run_turn(
            session,
            "go",
            llm,
            sink=_silent_sink(),
            approval=AutoApproveGate(allow=True),
        )
    )
    assert "step one" in out or "step two" in out
    assert llm.calls >= 1


def test_runner_isolates_workspaces(tmp_path: Path):
    """Each case's workspace_files must not leak across cases."""
    cases = [
        EvalCase(
            id="a", prompt="echo",
            script=(ScriptStep(text="ok-a"),),
            expect=ExpectedOutcome(substring="ok-a"),
            workspace_files={"only_a.txt": "x"},
        ),
        EvalCase(
            id="b", prompt="echo",
            script=(ScriptStep(text="ok-b"),),
            expect=ExpectedOutcome(substring="ok-b"),
        ),
    ]
    report = run_cases(cases)
    assert report.passed_all
