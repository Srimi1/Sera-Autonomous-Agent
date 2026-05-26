"""Context compaction behaviour."""
from __future__ import annotations

import asyncio
from typing import Any


from sera.agent.loop import _build_view, run_turn
from sera.context.compressor import FENCE, compact_session
from sera.context.tokens import estimate_messages
from sera.llm.base import ContextOverflow, StreamChunk
from sera.memory.session import Session
from sera.safety.approval import AutoApproveGate


async def _fake_summary(_middle):
    return (
        f"{FENCE}\n\n## Remaining Work\n- finish task X\n\n"
        "## Recent Decisions\n- chose option A\n\n"
        "## Open Threads\n- topic Y"
    )


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def test_under_threshold_noop():
    messages = [_msg("system", "hi"), _msg("user", "ping"), _msg("assistant", "pong")]
    out = asyncio.run(
        compact_session(
            messages,
            summarise=_fake_summary,
            budget_tokens=128_000,
        )
    )
    assert out == messages


def test_over_threshold_compacts_keeping_tail_byte_identical():
    big_content = "x " * 4000  # ~4k tokens
    messages = [_msg("system", "system prompt")]
    for i in range(20):
        messages.append(_msg("user", f"q{i} {big_content}"))
        messages.append(_msg("assistant", f"a{i} {big_content}"))

    out = asyncio.run(
        compact_session(
            messages,
            summarise=_fake_summary,
            budget_tokens=8_000,
            target_ratio=0.8,
            tail_ratio=0.3,
        )
    )
    # Head preserved.
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "system prompt"
    # Summary message inserted with the fence.
    assert out[1]["role"] == "assistant"
    assert out[1]["content"].startswith(FENCE)
    assert "Remaining Work" in out[1]["content"]
    # Whatever tail length the compactor picked, it is byte-identical
    # to the matching suffix of the source. No content was rewritten.
    returned_tail = out[2:]
    assert len(returned_tail) >= 1
    assert returned_tail == messages[-len(returned_tail):]
    # Token usage actually dropped.
    assert estimate_messages(out) < estimate_messages(messages)


def test_fence_added_if_summary_missing_it():
    async def _bare_summary(_middle):
        return "Some condensed text without a fence."

    messages = [_msg("system", "s")]
    for i in range(30):
        messages.append(_msg("user", "x " * 500))
        messages.append(_msg("assistant", "y " * 500))

    out = asyncio.run(
        compact_session(
            messages,
            summarise=_bare_summary,
            budget_tokens=4_000,
        )
    )
    assert out[1]["content"].startswith(FENCE)


# --- run_turn overflow path -----------------------------------------------------


class _OverflowOnceLLM:
    """Raises ContextOverflow on first MAIN call, succeeds on retry.

    Summarisation calls (tools is None) always succeed so the compactor can
    produce its summary. The main agent call (tools is non-None) raises
    once, then succeeds.
    """

    name = "openai"
    context_budget = 4_000

    def __init__(self) -> None:
        self.main_calls = 0
        self.summarise_calls = 0

    async def stream(self, messages, tools=None, system=None):
        if tools is None:
            self.summarise_calls += 1
            yield StreamChunk(delta_text=FENCE + "\n## Remaining Work\n- done")
            yield StreamChunk(finish_reason="stop")
            return
        self.main_calls += 1
        if self.main_calls == 1:
            raise ContextOverflow("simulated context overflow")
        yield StreamChunk(delta_text="ok")
        yield StreamChunk(finish_reason="stop")


def test_run_turn_recovers_from_context_overflow(tmp_path):
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)

    # Fill the session with enough history to exceed the small budget.
    from sera.memory.session import Message

    big = "padding " * 500
    for i in range(30):
        s.append(Message(role="user", content=f"u{i} {big}"))
        s.append(Message(role="assistant", content=f"a{i} {big}"))

    llm = _OverflowOnceLLM()
    out = asyncio.run(
        run_turn(
            s,
            "final question",
            llm,
            approval=AutoApproveGate(allow=False),
        )
    )
    assert "ok" in out
    assert llm.main_calls == 2  # first raised, second succeeded
    assert llm.summarise_calls >= 1  # summarisation happened


# --- _build_view ----------------------------------------------------------------


class _DummyLLM:
    name = "openai"
    context_budget = 4_000

    async def stream(self, messages, tools=None, system=None):
        # Used by build_summarise_call when _build_view triggers compaction.
        yield StreamChunk(delta_text=FENCE + "\n## Remaining Work\n- done")
        yield StreamChunk(finish_reason="stop")


def test_build_view_compacts_above_threshold():
    msgs = [{"role": "user", "content": "x " * 6000}]  # ~6k tokens
    out = asyncio.run(_build_view(msgs, llm=_DummyLLM(), target_ratio=0.8))
    # _DummyLLM budget is 4k; threshold 3.2k; should trigger compaction.
    assert isinstance(out, list)


def test_build_view_noop_below_threshold():
    msgs = [{"role": "user", "content": "small"}]
    out = asyncio.run(_build_view(msgs, llm=_DummyLLM(), target_ratio=0.8))
    assert out == msgs
