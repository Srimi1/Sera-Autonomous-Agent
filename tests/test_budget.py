"""P-07: shared IterationBudget with one-shot grace + end-to-end loop."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.agent.budget import IterationBudget, MaxIterations
from sera.agent.loop import GRACE_NOTICE, run_turn
from sera.llm.base import StreamChunk
from sera.memory.session import Session


def test_budget_counts_down():
    b = IterationBudget.of(3)
    assert b.remaining == 3 and b.total == 3 and not b.grace_used
    b.consume()
    b.consume()
    b.consume()
    assert b.remaining == 0


def test_budget_raises_after_exhaustion():
    b = IterationBudget.of(1)
    b.consume()
    with pytest.raises(MaxIterations):
        b.consume()


def test_budget_grace_refunds_one_iteration():
    b = IterationBudget.of(2)
    b.consume()
    b.consume()
    assert b.can_request_grace()
    b.request_grace()
    assert b.grace_used and b.remaining == 1
    b.consume()
    assert b.remaining == 0
    assert not b.can_request_grace()
    with pytest.raises(MaxIterations):
        b.consume()


def test_budget_grace_one_shot():
    b = IterationBudget.of(1)
    b.consume()
    b.request_grace()
    with pytest.raises(RuntimeError):
        b.request_grace()


def test_budget_rejects_zero_total():
    with pytest.raises(ValueError):
        IterationBudget.of(0)


class _ToolLoopLLM:
    """Streams a tool call every turn until it sees GRACE_NOTICE; then summarizes."""

    name = "openai"
    context_budget = 8000

    def __init__(self) -> None:
        self.turns = 0
        self.tool_schemas_seen: list[bool] = []

    async def stream(self, messages, tools=None, system=None):
        self.turns += 1
        self.tool_schemas_seen.append(bool(tools))

        saw_grace = any(
            isinstance(m.get("content"), str) and GRACE_NOTICE in m["content"]
            for m in messages
        )
        if saw_grace:
            yield StreamChunk(delta_text="final answer.")
            yield StreamChunk(finish_reason="stop")
            return

        yield StreamChunk(delta_text=f"step {self.turns}… ")
        yield StreamChunk(
            tool_call_delta={
                "id": f"tc{self.turns}",
                "name": "file_read",
                "arguments": {"path": "/tmp/nope"},
            }
        )
        yield StreamChunk(finish_reason="tool_calls")


def test_budget_triggers_grace_in_run_turn(tmp_path: Path):
    """Budget of 2 + a model that always tool-calls → grace turn produces final text."""
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    llm = _ToolLoopLLM()
    budget = IterationBudget.of(2)

    out = asyncio.run(
        run_turn(
            session,
            "loop forever",
            llm,
            budget=budget,
            max_iterations=99,  # ignored: explicit budget wins
        )
    )

    assert "final answer" in out
    assert budget.grace_used
    # Two normal turns + one grace turn = 3 total stream calls.
    assert llm.turns == 3
    # Grace turn must be tool-less.
    assert llm.tool_schemas_seen == [True, True, False]
    # GRACE_NOTICE is persisted as a user message in the session history.
    assert any(
        m.role == "user" and GRACE_NOTICE in (m.content or "")
        for m in session.messages
    )


class _ForeverToolLLM:
    """Never stops tool-calling, even after grace notice."""

    name = "openai"
    context_budget = 8000

    def __init__(self) -> None:
        self.turns = 0

    async def stream(self, messages, tools=None, system=None):
        self.turns += 1
        yield StreamChunk(delta_text=f"t{self.turns}")
        yield StreamChunk(
            tool_call_delta={
                "id": f"tc{self.turns}",
                "name": "file_read",
                "arguments": {"path": "/tmp/x"},
            }
        )
        yield StreamChunk(finish_reason="tool_calls")


def test_grace_turn_breaks_even_if_model_keeps_tooling(tmp_path: Path):
    """Even if the model emits tool calls during the grace turn, we exit cleanly."""
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    llm = _ForeverToolLLM()
    budget = IterationBudget.of(1)

    out = asyncio.run(
        run_turn(session, "loop", llm, budget=budget),
    )
    assert budget.grace_used
    # Normal turn + grace turn = 2; loop must not continue past grace.
    assert llm.turns == 2
    assert out  # some text was captured
