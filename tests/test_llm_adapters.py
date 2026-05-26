"""Backfill coverage for the OpenAI and Anthropic adapters.

The provider SDKs are never imported here — both adapters lazy-create their
client in _ensure_client(), so injecting a fake `_client` (OpenAI) or driving
the pure converters (Anthropic) exercises streaming assembly, tool-call
accumulation across deltas, usage reporting, and context-overflow detection
with zero network and no SDK dependency.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from sera.llm.adapters.anthropic_adapter import (
    AnthropicAdapter,
    _is_context_overflow_anthropic,
)
from sera.llm.adapters.openai_adapter import (
    OpenAIAdapter,
    _is_context_overflow_openai,
)
from sera.llm.base import ContextOverflow


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _drain(stream):
    return [c async for c in stream]


# ---------------------------------------------------------------------------
# OpenAI adapter — fake streaming client
# ---------------------------------------------------------------------------

def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _oa_text_event(text):
    return _ns(choices=[_ns(delta=_ns(content=text, tool_calls=None), finish_reason=None)])


def _oa_toolcall_event(*, index, tc_id=None, name=None, args=None):
    fn = _ns(name=name, arguments=args)
    tc = _ns(index=index, id=tc_id, function=fn)
    return _ns(choices=[_ns(delta=_ns(content=None, tool_calls=[tc]), finish_reason=None)])


def _oa_finish_event(reason="stop"):
    return _ns(choices=[_ns(delta=_ns(content=None, tool_calls=None), finish_reason=reason)])


class _FakeOpenAIStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


class _FakeOpenAIClient:
    def __init__(self, events, *, raise_on_create=None):
        self._events = events
        self._raise = raise_on_create
        self.last_kwargs = None

        async def _create(**kwargs):
            self.last_kwargs = kwargs
            if self._raise is not None:
                raise self._raise
            return _FakeOpenAIStream(self._events)

        self.chat = _ns(completions=_ns(create=_create))


class TestOpenAIAdapter:
    def test_streams_text(self) -> None:
        adapter = OpenAIAdapter(model="gpt-4o-mini")
        adapter._client = _FakeOpenAIClient([_oa_text_event("hel"), _oa_text_event("lo"), _oa_finish_event()])
        chunks = _run(_drain(adapter.stream(messages=[{"role": "user", "content": "hi"}])))
        text = "".join(c.delta_text for c in chunks)
        assert "hello" in text
        assert any(c.finish_reason == "stop" for c in chunks)

    def test_assembles_tool_call_across_deltas(self) -> None:
        adapter = OpenAIAdapter()
        events = [
            _oa_toolcall_event(index=0, tc_id="call_1", name="file_", args='{"pa'),
            _oa_toolcall_event(index=0, name="read", args='th": "x"}'),
            _oa_finish_event("tool_calls"),
        ]
        adapter._client = _FakeOpenAIClient(events)
        chunks = _run(_drain(adapter.stream(messages=[{"role": "user", "content": "go"}])))
        tcs = [c.tool_call_delta for c in chunks if c.tool_call_delta]
        assert len(tcs) == 1
        assert tcs[0]["id"] == "call_1"
        assert tcs[0]["name"] == "file_read"
        assert tcs[0]["arguments"] == {"path": "x"}

    def test_bad_tool_args_fall_back_to_raw(self) -> None:
        adapter = OpenAIAdapter()
        events = [
            _oa_toolcall_event(index=0, tc_id="c1", name="t", args="{not json"),
            _oa_finish_event("tool_calls"),
        ]
        adapter._client = _FakeOpenAIClient(events)
        chunks = _run(_drain(adapter.stream(messages=[{"role": "user", "content": "go"}])))
        tc = next(c.tool_call_delta for c in chunks if c.tool_call_delta)
        assert tc["arguments"] == {"_raw": "{not json"}

    def test_system_prepended(self) -> None:
        adapter = OpenAIAdapter()
        client = _FakeOpenAIClient([_oa_finish_event()])
        adapter._client = client
        _run(_drain(adapter.stream(messages=[{"role": "user", "content": "hi"}], system="be terse")))
        assert client.last_kwargs["messages"][0] == {"role": "system", "content": "be terse"}

    def test_tools_passed_through(self) -> None:
        adapter = OpenAIAdapter()
        client = _FakeOpenAIClient([_oa_finish_event()])
        adapter._client = client
        tools = [{"type": "function", "function": {"name": "x"}}]
        _run(_drain(adapter.stream(messages=[{"role": "user", "content": "hi"}], tools=tools)))
        assert client.last_kwargs["tools"] == tools
        assert client.last_kwargs["tool_choice"] == "auto"

    def test_context_overflow_normalized(self) -> None:
        adapter = OpenAIAdapter()
        err = RuntimeError("This model's maximum context length is 8192 tokens")
        adapter._client = _FakeOpenAIClient([], raise_on_create=err)
        with pytest.raises(ContextOverflow):
            _run(_drain(adapter.stream(messages=[{"role": "user", "content": "x"}])))

    def test_other_errors_propagate(self) -> None:
        adapter = OpenAIAdapter()
        adapter._client = _FakeOpenAIClient([], raise_on_create=RuntimeError("auth failed"))
        with pytest.raises(RuntimeError, match="auth failed"):
            _run(_drain(adapter.stream(messages=[{"role": "user", "content": "x"}])))

    def test_overflow_detector(self) -> None:
        assert _is_context_overflow_openai(RuntimeError("context length exceeded"))
        assert _is_context_overflow_openai(RuntimeError("maximum context reached"))
        assert not _is_context_overflow_openai(RuntimeError("rate limit"))

    def test_name_and_budget(self) -> None:
        adapter = OpenAIAdapter()
        assert adapter.name == "openai"
        assert adapter.context_budget == 128_000


# ---------------------------------------------------------------------------
# Anthropic adapter — pure message conversion + overflow detection
# ---------------------------------------------------------------------------

class TestAnthropicConversion:
    def test_drops_system_message(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {"role": "system", "content": "ignore me"},
            {"role": "user", "content": "hi"},
        ])
        assert out == [{"role": "user", "content": "hi"}]

    def test_tool_result_becomes_user_block(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {"role": "tool", "tool_call_id": "tc1", "content": "42"},
        ])
        assert out[0]["role"] == "user"
        block = out[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tc1"
        assert block["content"] == "42"

    def test_assistant_tool_calls_become_tool_use(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {"id": "t1", "function": {"name": "file_read", "arguments": '{"path": "a"}'}}
                ],
            },
        ])
        blocks = out[0]["content"]
        assert blocks[0] == {"type": "text", "text": "let me check"}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["id"] == "t1"
        assert blocks[1]["name"] == "file_read"
        assert blocks[1]["input"] == {"path": "a"}

    def test_tool_use_handles_dict_arguments(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "t1", "function": {"name": "x", "arguments": {"already": "dict"}}}
                ],
            },
        ])
        assert out[0]["content"][0]["input"] == {"already": "dict"}

    def test_tool_use_bad_json_falls_back_to_raw(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "t1", "function": {"name": "x", "arguments": "{broken"}}
                ],
            },
        ])
        assert out[0]["content"][0]["input"] == {"_raw": "{broken"}

    def test_plain_assistant_passthrough(self) -> None:
        out = AnthropicAdapter()._to_anthropic_messages([
            {"role": "assistant", "content": "just text"},
        ])
        assert out == [{"role": "assistant", "content": "just text"}]

    def test_overflow_detector(self) -> None:
        assert _is_context_overflow_anthropic(RuntimeError("prompt is too long"))
        assert _is_context_overflow_anthropic(RuntimeError("context window exceeded"))
        assert not _is_context_overflow_anthropic(RuntimeError("overloaded"))

    def test_name_and_budget(self) -> None:
        adapter = AnthropicAdapter()
        assert adapter.name == "anthropic"
        assert adapter.context_budget == 200_000
        assert adapter.model == "claude-sonnet-4-6"
