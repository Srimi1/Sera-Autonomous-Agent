from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from sera.agent.loop import run_turn
from sera.llm.base import StreamChunk
from sera.memory.session import Session
from sera.profile import build_profile_prompt, profile_path, render_profile


class _PromptCaptureLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    def __init__(self) -> None:
        self.systems: list[str | None] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools=None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.systems.append(system)
        yield StreamChunk(delta_text="ok")
        yield StreamChunk(finish_reason="stop")


def test_render_profile_creates_managed_blocks():
    rendered = render_profile("")
    assert "# User Profile" in rendered
    assert "<!-- sera:style:start -->" in rendered
    assert "<!-- sera:current-priorities:end -->" in rendered


def test_render_profile_preserves_manual_text():
    existing = "# User Profile\n\nPersonal note.\n"
    rendered = render_profile(existing)
    assert "Personal note." in rendered
    assert "<!-- sera:tooling:start -->" in rendered


def test_build_profile_prompt_reads_workspace_file(tmp_path: Path):
    profile = profile_path(tmp_path)
    profile.write_text("# User Profile\n\n- terse\n")
    prompt = build_profile_prompt(tmp_path)
    assert "### PROFILE.md" in prompt
    assert "terse" in prompt


def test_run_turn_injects_profile_into_system_prompt(tmp_path: Path, monkeypatch):
    import sera.memory.session as session_mod

    monkeypatch.setattr(session_mod, "_LOCKS_DIR", tmp_path / "locks")
    profile = profile_path(tmp_path)
    profile.write_text("# User Profile\n\n- prefer terse answers\n")
    llm = _PromptCaptureLLM()
    session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "sessions.db")
    text = asyncio.run(run_turn(session, "hello", llm))
    assert text == "ok"
    assert llm.systems
    assert "### PROFILE.md" in (llm.systems[0] or "")
    assert "prefer terse answers" in (llm.systems[0] or "")
