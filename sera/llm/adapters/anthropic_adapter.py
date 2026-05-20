"""Anthropic adapter. Streams native tool_use blocks."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from sera.llm.base import ContextOverflow, StreamChunk
from sera.llm.cache import apply_cache_control_anthropic, parse_anthropic_usage
from sera.llm.secrets import get_key


def _is_context_overflow_anthropic(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "prompt is too long" in msg
        or "max_tokens" in msg and "exceed" in msg
        or "context window" in msg
    )


class AnthropicAdapter:
    name = "anthropic"
    context_budget = 200_000

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.model = model
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            key = get_key("anthropic")
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY missing. Run `sera setup` or export the env var."
                )
            self._client = AsyncAnthropic(api_key=key)
        return self._client

    def _to_anthropic_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style messages to Anthropic format."""
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                continue  # handled separately
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m["tool_call_id"],
                                "content": m.get("content", ""),
                            }
                        ],
                    }
                )
                continue
            if role == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    args = tc["function"]["arguments"]
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": args,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": role, "content": m.get("content", "")})
        return out

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        client = self._ensure_client()
        sys_text = system
        if not sys_text:
            for m in messages:
                if m.get("role") == "system":
                    sys_text = m.get("content", "")
                    break

        anth_messages = self._to_anthropic_messages(messages)
        if sys_text:
            system_blocks, anth_messages = apply_cache_control_anthropic(
                sys_text, anth_messages
            )
        else:
            system_blocks = []

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anth_messages,
            "max_tokens": 4096,
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = tools

        tool_buf: dict[int, dict[str, Any]] = {}

        # The Anthropic SDK raises 4xx inside the `async with` body, not at
        # context-manager construction. The try/except must wrap the body.
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", "") == "tool_use":
                            tool_buf[event.index] = {
                                "id": block.id,
                                "name": block.name,
                                "arguments_raw": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.delta
                        dtype = getattr(delta, "type", "")
                        if dtype == "text_delta":
                            yield StreamChunk(delta_text=delta.text)
                        elif dtype == "input_json_delta":
                            if event.index in tool_buf:
                                tool_buf[event.index]["arguments_raw"] += delta.partial_json
                    elif etype == "message_stop":
                        for buf in tool_buf.values():
                            try:
                                parsed = json.loads(buf["arguments_raw"] or "{}")
                            except json.JSONDecodeError:
                                parsed = {"_raw": buf["arguments_raw"]}
                            yield StreamChunk(
                                tool_call_delta={
                                    "id": buf["id"],
                                    "name": buf["name"],
                                    "arguments": parsed,
                                }
                            )
                        final = await stream.get_final_message()
                        usage = parse_anthropic_usage(getattr(final, "usage", None))
                        yield StreamChunk(
                            finish_reason=final.stop_reason or "stop",
                            usage={
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "cache_read_input_tokens": usage.cache_read_input_tokens,
                                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                            },
                        )
        except Exception as e:  # noqa: BLE001 — normalize provider errors
            if _is_context_overflow_anthropic(e):
                raise ContextOverflow(str(e)) from e
            raise
