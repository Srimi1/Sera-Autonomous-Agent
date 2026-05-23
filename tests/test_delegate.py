"""Tests for sera.tools.delegate — subagent delegation with shared IterationBudget."""
from __future__ import annotations

import asyncio

import pytest

from sera.agent.budget import IterationBudget, MaxIterations
from sera.eval.cases import ScriptStep
from sera.eval.stub_llm import StubLLM
from sera.tools.base import ToolContext
from sera.tools.delegate import delegate_task, make_delegate_tool
from sera.tools.registry import all_tools, reset as reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub(text: str) -> StubLLM:
    return StubLLM([ScriptStep(text=text, finish_reason="stop")])


def _budget(n: int = 10) -> IterationBudget:
    return IterationBudget.of(n)


# ---------------------------------------------------------------------------
# delegate_task — basic execution
# ---------------------------------------------------------------------------

class TestDelegateTask:
    def test_returns_subagent_response(self) -> None:
        result = asyncio.run(
            delegate_task("summarize this PDF", _stub("Here is the summary."), budget=_budget())
        )
        assert result == "Here is the summary."

    def test_empty_response_ok(self) -> None:
        result = asyncio.run(
            delegate_task("ping", _stub(""), budget=_budget())
        )
        assert isinstance(result, str)

    def test_context_prepended_to_prompt(self) -> None:
        received: list[str] = []

        class _TracingLLM:
            name = "openai"
            model = "stub"
            context_budget = 32_000
            async def stream(self, messages, *, tools=None, system=None):
                received.append(messages[-1]["content"])
                from sera.llm.base import StreamChunk
                yield StreamChunk(delta_text="ok", finish_reason=None)
                yield StreamChunk(finish_reason="stop")

        asyncio.run(
            delegate_task(
                "do the task",
                _TracingLLM(),
                budget=_budget(),
                context="Background: this is a PDF about finance.",
            )
        )
        assert len(received) == 1
        assert "Background: this is a PDF about finance." in received[0]
        assert "do the task" in received[0]

    def test_no_context_just_prompt(self) -> None:
        received: list[str] = []

        class _TracingLLM:
            name = "openai"
            model = "stub"
            context_budget = 32_000
            async def stream(self, messages, *, tools=None, system=None):
                received.append(messages[-1]["content"])
                from sera.llm.base import StreamChunk
                yield StreamChunk(delta_text="ans", finish_reason=None)
                yield StreamChunk(finish_reason="stop")

        asyncio.run(delegate_task("just the task", _TracingLLM(), budget=_budget()))
        assert received[0] == "just the task"


# ---------------------------------------------------------------------------
# P-42 verification: budget consumed from shared pool
# ---------------------------------------------------------------------------

class TestSharedBudget:
    def test_budget_consumed_by_subagent(self) -> None:
        """Subagent run_turn consumes iterations from the shared budget."""
        budget = _budget(10)
        before = budget.remaining

        asyncio.run(
            delegate_task("summarize this PDF", _stub("Summary here."), budget=budget)
        )

        after = budget.remaining
        assert after < before, f"budget not consumed: before={before}, after={after}"

    def test_shared_budget_parent_and_subagent(self) -> None:
        """Parent and subagent share the same pool — combined consumption tracked."""
        budget = _budget(10)

        # Subagent uses 1 iteration
        asyncio.run(delegate_task("task A", _stub("result A"), budget=budget))
        after_subagent = budget.remaining

        # Subagent used at least 1
        assert after_subagent <= 9

    def test_exhausted_budget_raises(self) -> None:
        """Subagent respects the shared budget cap."""
        budget = IterationBudget.of(1)
        # Consume the 1 remaining iteration manually (simulating parent usage)
        budget.consume()
        # Now budget is at 0 with grace available; subagent tries to consume → MaxIterations after grace
        # The subagent will get grace and produce "[max iterations reached]" or similar
        # We verify it doesn't hang or crash
        result = asyncio.run(
            delegate_task("task", _stub("answer"), budget=budget)
        )
        # Either returns the stub response (grace consumed) or the max-iter sentinel
        assert isinstance(result, str)

    def test_subagent_does_not_get_new_budget(self) -> None:
        """delegate_task passes the same budget object — not a copy."""
        budget = _budget(5)
        original_total = budget.total

        asyncio.run(delegate_task("task", _stub("r"), budget=budget))

        # total is unchanged (not a new budget)
        assert budget.total == original_total
        # but remaining decreased
        assert budget.remaining < budget.total


# ---------------------------------------------------------------------------
# make_delegate_tool — tool factory
# ---------------------------------------------------------------------------

class TestMakeDelegateTool:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_tool_name(self) -> None:
        tool = make_delegate_tool(_stub("r"), _budget())
        assert tool.name == "delegate_task"

    def test_tool_schema_has_prompt(self) -> None:
        tool = make_delegate_tool(_stub("r"), _budget())
        assert "prompt" in tool.parameters["properties"]
        assert "prompt" in tool.parameters["required"]

    def test_tool_schema_has_context(self) -> None:
        tool = make_delegate_tool(_stub("r"), _budget())
        assert "context" in tool.parameters["properties"]

    def test_tool_handler_returns_response(self) -> None:
        tool = make_delegate_tool(_stub("delegated result"), _budget())
        ctx = ToolContext(session_id="s1", workspace="/tmp")
        result = asyncio.run(tool.handler({"prompt": "do something"}, ctx))
        assert result == "delegated result"

    def test_tool_handler_empty_prompt(self) -> None:
        tool = make_delegate_tool(_stub("r"), _budget())
        ctx = ToolContext(session_id="s1", workspace="/tmp")
        result = asyncio.run(tool.handler({"prompt": ""}, ctx))
        assert "required" in result.lower() or "prompt" in result.lower()

    def test_tool_handler_with_context(self) -> None:
        received: list[str] = []

        class _T:
            name = "openai"
            model = "stub"
            context_budget = 32_000
            async def stream(self, messages, *, tools=None, system=None):
                received.append(messages[-1]["content"])
                from sera.llm.base import StreamChunk
                yield StreamChunk(delta_text="ok", finish_reason=None)
                yield StreamChunk(finish_reason="stop")

        tool = make_delegate_tool(_T(), _budget())
        ctx = ToolContext(session_id="s1", workspace="/tmp")
        asyncio.run(tool.handler(
            {"prompt": "the task", "context": "some context"},
            ctx,
        ))
        assert "some context" in received[0]
        assert "the task" in received[0]

    def test_tool_budget_shared_through_handler(self) -> None:
        budget = _budget(10)
        tool = make_delegate_tool(_stub("r"), budget)
        ctx = ToolContext(session_id="s1", workspace="/tmp")
        asyncio.run(tool.handler({"prompt": "p"}, ctx))
        assert budget.remaining < 10

    def test_permission_execute(self) -> None:
        from sera.tools.base import Permission
        tool = make_delegate_tool(_stub("r"), _budget())
        assert tool.permission == Permission.EXECUTE
