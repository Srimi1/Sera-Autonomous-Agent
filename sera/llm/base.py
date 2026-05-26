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
    """One streaming event from a provider.

    `usage` is set at most once per stream — on the final event — and carries
    the provider's reported token counts. None on intermediate chunks.
    """

    delta_text: str = ""
    tool_call_delta: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"


class LLM(Protocol):
    name: str
    context_budget: int

    # Declared as a plain (non-async) method returning AsyncIterator: the
    # adapters implement `stream` as async generators (`async def` + `yield`),
    # whose call-type is AsyncIterator[StreamChunk] directly — NOT a coroutine.
    # Marking this `async def` would make mypy infer Coroutine[..., AsyncIterator]
    # and reject both `async for chunk in llm.stream(...)` and adapter conformance.
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]: ...
