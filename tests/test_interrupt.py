"""P-07: InterruptToken cancels in-flight turns."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.agent.budget import IterationBudget
from sera.agent.interrupt import InterruptToken, Interrupted, install_sigint
from sera.agent.loop import run_turn
from sera.llm.base import StreamChunk
from sera.memory.session import Session


def test_token_check_raises_when_set():
    t = InterruptToken()
    t.check()  # no-op
    t.set()
    assert t.is_set()
    with pytest.raises(Interrupted):
        t.check()


def test_install_sigint_routes_to_token():
    import os
    import signal

    token = InterruptToken()
    with install_sigint(token):
        os.kill(os.getpid(), signal.SIGINT)
    assert token.is_set()


def test_install_sigint_restores_handler():
    import signal

    original = signal.getsignal(signal.SIGINT)
    token = InterruptToken()
    with install_sigint(token):
        assert signal.getsignal(signal.SIGINT) is not original
    assert signal.getsignal(signal.SIGINT) is original


class _PreSetToolLLM:
    """Streams one tool call so the loop reaches a post-tool interrupt check."""

    name = "openai"
    context_budget = 8000

    def __init__(self, token: InterruptToken) -> None:
        self.token = token
        self.turns = 0

    async def stream(self, messages, tools=None, system=None):
        self.turns += 1
        yield StreamChunk(delta_text="working… ")
        yield StreamChunk(
            tool_call_delta={
                "id": "tc1",
                "name": "file_read",
                "arguments": {"path": "/tmp/nope"},
            }
        )
        # Fire the cancel as the stream ends — loop checks `interrupt.check()`
        # right after the tool result, which is where we want to bail.
        self.token.set()
        yield StreamChunk(finish_reason="tool_calls")


def test_interrupt_cancels_mid_loop(tmp_path: Path):
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    token = InterruptToken()
    llm = _PreSetToolLLM(token)

    with pytest.raises(Interrupted):
        asyncio.run(
            run_turn(
                session,
                "do thing",
                llm,
                interrupt=token,
                budget=IterationBudget.of(10),
            )
        )
    # Only one LLM round happened — the second-round budget check tripped the token.
    assert llm.turns == 1
    # Session still has the user message + assistant turn + tool result persisted.
    roles = [m.role for m in session.messages]
    assert "user" in roles and "assistant" in roles and "tool" in roles


def test_pre_set_token_aborts_before_any_call(tmp_path: Path):
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    token = InterruptToken()
    token.set()

    class _Unused:
        name = "openai"
        context_budget = 8000

        async def stream(self, messages, tools=None, system=None):
            yield StreamChunk(delta_text="should not run")

    llm = _Unused()
    with pytest.raises(Interrupted):
        asyncio.run(run_turn(session, "msg", llm, interrupt=token))
