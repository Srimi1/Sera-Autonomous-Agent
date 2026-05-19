"""OpenAI adapter. Streams ChatCompletion with native tool calls."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from sera.llm.base import ContextOverflow, StreamChunk
from sera.llm.secrets import get_key


def _is_context_overflow_openai(e: Exception) -> bool:
    """Detect OpenAI's context-length error across SDK versions."""
    code = getattr(e, "code", None) or getattr(getattr(e, "body", None), "get", lambda *_: None)("code")
    if code == "context_length_exceeded":
        return True
    msg = str(e).lower()
    return "context length" in msg or "maximum context" in msg or "context_length" in msg


class OpenAIAdapter:
    name = "openai"
    context_budget = 128_000

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            key = get_key("openai")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY missing. Run `sera setup` or export the env var."
                )
            self._client = AsyncOpenAI(api_key=key)
        return self._client

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        client = self._ensure_client()
        msgs = list(messages)
        if system and (not msgs or msgs[0].get("role") != "system"):
            msgs = [{"role": "system", "content": system}, *msgs]
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs, "stream": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Per-tool-call assembly across deltas.
        tool_buf: dict[int, dict[str, Any]] = {}

        try:
            stream = await client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — normalize provider errors
            if _is_context_overflow_openai(e):
                raise ContextOverflow(str(e)) from e
            raise

        async for event in stream:
            if not event.choices:
                continue
            choice = event.choices[0]
            delta = choice.delta
            if delta.content:
                yield StreamChunk(delta_text=delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    buf = tool_buf.setdefault(
                        idx, {"id": None, "name": "", "arguments": ""}
                    )
                    if tc.id:
                        buf["id"] = tc.id
                    if tc.function and tc.function.name:
                        buf["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        buf["arguments"] += tc.function.arguments
            if choice.finish_reason:
                # Flush completed tool calls.
                for buf in tool_buf.values():
                    try:
                        parsed_args = json.loads(buf["arguments"] or "{}")
                    except json.JSONDecodeError:
                        parsed_args = {"_raw": buf["arguments"]}
                    yield StreamChunk(
                        tool_call_delta={
                            "id": buf["id"],
                            "name": buf["name"],
                            "arguments": parsed_args,
                        }
                    )
                yield StreamChunk(finish_reason=choice.finish_reason)
