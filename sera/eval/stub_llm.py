"""Deterministic LLM stub that replays a case's `script` step by step.

Each `_consume` in the agent loop pulls one ScriptStep. Text streams as a
single delta, tool calls are emitted as `tool_call_delta` chunks, and the
final chunk carries the step's `finish_reason` plus zero-valued usage.

When the script is exhausted the stub returns an empty `stop` turn so the
loop terminates rather than hanging on an empty generator.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from sera.eval.cases import ScriptStep
from sera.llm.base import StreamChunk


class StubLLM:
    name = "openai"  # tool-call schema branch matches OpenAI shape
    context_budget = 32_000

    def __init__(self, script: list[ScriptStep] | tuple[ScriptStep, ...]) -> None:
        self._steps = list(script)
        self._cursor = 0
        self.calls = 0
        self.received_system: list[str | None] = []

    @property
    def model(self) -> str:
        return "stub"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        self.received_system.append(system)
        step = (
            self._steps[self._cursor]
            if self._cursor < len(self._steps)
            else ScriptStep(text="", finish_reason="stop")
        )
        self._cursor += 1

        if step.text:
            yield StreamChunk(delta_text=step.text)
        for tc in step.tool_calls:
            yield StreamChunk(tool_call_delta=dict(tc))
        yield StreamChunk(
            finish_reason=step.effective_finish_reason,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )
