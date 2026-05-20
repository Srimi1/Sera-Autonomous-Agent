"""Pass/fail scoring for a single case."""
from __future__ import annotations

from dataclasses import dataclass

from sera.eval.cases import EvalCase
from sera.memory.session import Session


@dataclass(frozen=True)
class ScoreVerdict:
    passed: bool
    reason: str = ""


def _collected_tool_calls(session: Session) -> list[str]:
    """Names of every tool call made by the assistant in this session."""
    out: list[str] = []
    for m in session.messages:
        if m.role != "assistant":
            continue
        for tc in m.tool_calls or []:
            fn = (tc.get("function") or {})
            name = fn.get("name") or tc.get("name")
            if name:
                out.append(name)
    return out


def _final_text(session: Session) -> str:
    """Concatenation of assistant content across the turn (final answer dominates)."""
    parts: list[str] = []
    for m in session.messages:
        if m.role == "assistant" and m.content:
            parts.append(m.content)
    return "\n".join(parts)


def score(case: EvalCase, session: Session, *, iterations: int) -> ScoreVerdict:
    exp = case.expect
    final = _final_text(session)
    tool_calls = _collected_tool_calls(session)

    if exp.substring and exp.substring.lower() not in final.lower():
        return ScoreVerdict(False, f"substring {exp.substring!r} not in output")

    for required in exp.tool_calls:
        if required not in tool_calls:
            return ScoreVerdict(False, f"expected tool {required!r} not called")

    for forbidden in exp.forbid_tool_calls:
        if forbidden in tool_calls:
            return ScoreVerdict(False, f"forbidden tool {forbidden!r} was called")

    if exp.min_iterations and iterations < exp.min_iterations:
        return ScoreVerdict(
            False, f"only {iterations} iterations (min {exp.min_iterations})"
        )
    if exp.max_iterations and iterations > exp.max_iterations:
        return ScoreVerdict(
            False, f"{iterations} iterations (max {exp.max_iterations})"
        )

    return ScoreVerdict(True)
