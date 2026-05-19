"""LLM provider abstraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


class LLMError(Exception):
    """Base for provider errors normalized by adapters."""


class ContextOverflow(LLMError):
    """The request exceeded the model's context window.

    Adapters raise this on provider-specific 400s (OpenAI
    `context_length_exceeded`, Anthropic `invalid_request_error` with a
    max-tokens message, etc.). The agent loop catches it and retries with
    aggressive compaction.
    """


@dataclass
class StreamChunk:
    """One streaming event from a provider."""

    delta_text: str = ""
    tool_call_delta: dict[str, Any] | None = None
    finish_reason: str | None = None


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"


class LLM(Protocol):
    name: str
    context_budget: int

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]: ...
