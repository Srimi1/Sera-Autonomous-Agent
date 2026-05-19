"""Approval threshold wiring: config string → Permission enum + above-only gating."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.agent.loop import _effective_permission, run_turn
from sera.llm.base import StreamChunk
from sera.memory.session import Session
from sera.safety.approval import AutoApproveGate
from sera.tools.base import Permission, ToolCall


def test_parse_permission():
    assert Permission.parse("DANGEROUS") == Permission.DANGEROUS
    assert Permission.parse("execute") == Permission.EXECUTE
    assert Permission.parse(3) == Permission.EXECUTE
    assert Permission.parse(Permission.WRITE) == Permission.WRITE
    with pytest.raises(ValueError):
        Permission.parse("not-a-tier")


def test_effective_permission_escalates_dangerous_shell():
    safe = ToolCall(id="a", name="shell_run", arguments={"command": "ls"})
    bad = ToolCall(id="b", name="shell_run", arguments={"command": "rm -rf /"})
    assert _effective_permission(safe) == Permission.EXECUTE
    assert _effective_permission(bad) == Permission.DANGEROUS


class _FakeLLM:
    """Stub: emits one assistant text chunk + a write-tier tool call, then stops."""

    name = "openai"
    context_budget = 8000

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools=None, system=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(delta_text="planning… ")
            yield StreamChunk(
                tool_call_delta={"id": "tc1", "name": "file_write", "arguments": {
                    "path": "out.txt", "content": "hi"
                }}
            )
            yield StreamChunk(finish_reason="tool_calls")
        else:
            yield StreamChunk(delta_text="done.")
            yield StreamChunk(finish_reason="stop")


def test_threshold_dangerous_lets_write_pass(tmp_path: Path):
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    llm = _FakeLLM()
    gate = AutoApproveGate(allow=False)  # would deny if asked

    out = asyncio.run(
        run_turn(
            session,
            "make a file",
            llm,
            approval=gate,
            approval_threshold=Permission.DANGEROUS,
        )
    )
    assert "done" in out
    assert (tmp_path / "out.txt").read_text() == "hi"


def test_threshold_write_blocks_write(tmp_path: Path):
    db = tmp_path / "sessions.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    llm = _FakeLLM()
    gate = AutoApproveGate(allow=False)  # denies

    asyncio.run(
        run_turn(
            session,
            "make a file",
            llm,
            approval=gate,
            approval_threshold=Permission.WRITE,
        )
    )
    # File should NOT be written — gate denied.
    assert not (tmp_path / "out.txt").exists()
