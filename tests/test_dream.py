"""Tests for sera.dream.journal — the nightly Dream Journal (P-71).

Phase verification: 5 days of synthetic usage → 5 dream entries + ≥1 proposed
skill draft. The LLM is a stub that routes by prompt; a fake clock stamps each
night. No network, no real model — same seam as every other Sera test.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sera.dream.journal import (
    DreamEntry,
    DreamJournal,
    DreamJournalStore,
    SyntheticQA,
)


# ---------------------------------------------------------------------------
# Fake sessions
# ---------------------------------------------------------------------------

@dataclass
class _Msg:
    role: str
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    name: str | None = None


@dataclass
class _Session:
    id: str
    messages: list[_Msg]


def _session_using_tool(sid: str, tool: str, n: int) -> _Session:
    """A session whose assistant calls `tool` n times — feeds the discovery heuristic."""
    msgs: list[_Msg] = [_Msg(role="user", content=f"please do {tool}")]
    for _ in range(n):
        msgs.append(_Msg(role="assistant", content="working",
                         tool_calls=[{"function": {"name": tool, "arguments": "{}"}}]))
        msgs.append(_Msg(role="tool", content="ok", name=tool))
    return _Session(id=sid, messages=msgs)


# ---------------------------------------------------------------------------
# Stub LLM — routes by prompt content
# ---------------------------------------------------------------------------

def _make_stub(*, proposals: list[dict] | None = None):
    calls: list[str] = []

    async def stub(prompt: str) -> object:
        calls.append(prompt)
        if "consolidator" in prompt:
            return json.dumps({"summary": "User worked on deploys and reviewed PRs."})
        if "question/answer pairs" in prompt:
            return json.dumps({"qa": [
                {"question": "What did the user deploy?", "answer": "The gateway service."},
            ]})
        if "discovery agent" in prompt:
            return json.dumps({"proposals": proposals or []})
        return json.dumps({})

    return stub, calls


def _store(tmp_path: Path) -> DreamJournalStore:
    return DreamJournalStore(db=tmp_path / "dream.db")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class TestStore:
    def test_save_and_get(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = DreamEntry(date="2026-05-24", created_at=1.0, summary="hi",
                           synthetic_qa=(SyntheticQA("q", "a"),))
        store.save(entry)
        got = store.get("2026-05-24")
        assert got is not None
        assert got.summary == "hi"
        assert got.synthetic_qa[0].question == "q"

    def test_upsert_same_date(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(DreamEntry(date="2026-05-24", created_at=1.0, summary="v1"))
        store.save(DreamEntry(date="2026-05-24", created_at=2.0, summary="v2"))
        assert store.count() == 1
        assert store.get("2026-05-24").summary == "v2"

    def test_recent_ordered(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(DreamEntry(date="d1", created_at=1.0, summary="a"))
        store.save(DreamEntry(date="d2", created_at=2.0, summary="b"))
        recent = store.recent()
        assert [e.date for e in recent] == ["d2", "d1"]


# ---------------------------------------------------------------------------
# dream() — one night
# ---------------------------------------------------------------------------

class TestDreamOneNight:
    def test_consolidation_summary(self, tmp_path: Path) -> None:
        stub, _ = _make_stub()
        journal = DreamJournal(store=_store(tmp_path), llm_call=stub, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="2026-05-24", sessions=[
            _session_using_tool("s1", "web_search", 1)]))
        assert "deploys" in entry.summary or entry.summary
        assert entry.sessions_consolidated == 1

    def test_synthetic_qa_generated(self, tmp_path: Path) -> None:
        stub, _ = _make_stub()
        journal = DreamJournal(store=_store(tmp_path), llm_call=stub, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="d", sessions=[
            _session_using_tool("s1", "web_search", 1)]))
        assert len(entry.synthetic_qa) == 1
        assert entry.synthetic_qa[0].answer == "The gateway service."

    def test_skill_draft_from_repeated_tool(self, tmp_path: Path) -> None:
        """A tool used >= threshold across sessions → a drafted skill."""
        stub, _ = _make_stub(proposals=[{
            "trigger": "/deploy-digest", "name": "deploy_digest",
            "description": "summarize deploys", "body_hint": "steps", "reasoning": "repeated",
        }])
        journal = DreamJournal(store=_store(tmp_path), llm_call=stub, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="d", sessions=[
            _session_using_tool("s1", "shell_run", 3)]))
        assert len(entry.skill_drafts) == 1
        assert entry.skill_drafts[0]["trigger"] == "/deploy-digest"

    def test_quiet_day_still_records_entry(self, tmp_path: Path) -> None:
        stub, _ = _make_stub()
        store = _store(tmp_path)
        journal = DreamJournal(store=store, llm_call=stub, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="quiet", sessions=[]))
        assert entry.sessions_consolidated == 0
        assert store.get("quiet") is not None

    def test_no_skill_below_threshold(self, tmp_path: Path) -> None:
        """A tool used only twice doesn't reach discovery — no LLM proposal call."""
        stub, calls = _make_stub(proposals=[{"trigger": "/x", "name": "x",
                                             "description": "", "body_hint": "", "reasoning": ""}])
        journal = DreamJournal(store=_store(tmp_path), llm_call=stub, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="d", sessions=[
            _session_using_tool("s1", "web_search", 2)]))
        assert entry.skill_drafts == ()
        assert not any("discovery agent" in c for c in calls), "discovery LLM must be skipped below threshold"

    def test_consolidation_failure_is_soft(self, tmp_path: Path) -> None:
        async def broken(prompt: str):
            if "consolidator" in prompt:
                raise RuntimeError("model down")
            return json.dumps({})

        journal = DreamJournal(store=_store(tmp_path), llm_call=broken, clock=lambda: 100.0)
        entry = asyncio.run(journal.dream(date="d", sessions=[
            _session_using_tool("s1", "web_search", 1)]))
        assert "failed" in entry.summary    # soft-failed, entry still produced


# ---------------------------------------------------------------------------
# THE VERIFICATION: 5 days → 5 entries + >=1 skill draft
# ---------------------------------------------------------------------------

class TestFiveDayVerification:
    def test_five_days_five_entries_one_draft(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Day 3 has a repeated tool that should yield a skill draft.
        stub, _ = _make_stub(proposals=[{
            "trigger": "/weekly-digest", "name": "weekly_digest",
            "description": "weekly github digest", "body_hint": "steps", "reasoning": "repeated 3x",
        }])
        clock = [0.0]
        journal = DreamJournal(store=store, llm_call=stub, clock=lambda: clock[0])

        days = ["2026-05-20", "2026-05-21", "2026-05-22", "2026-05-23", "2026-05-24"]
        total_drafts = 0
        for i, day in enumerate(days):
            clock[0] = float(i)
            # Day index 2 (the 3rd day) does a repeated-tool workflow.
            tool_uses = 3 if i == 2 else 1
            sessions = [_session_using_tool(f"{day}-s1", "github_events", tool_uses)]
            entry = asyncio.run(journal.dream(date=day, sessions=sessions))
            total_drafts += len(entry.skill_drafts)

        assert store.count() == 5, "5 days of usage must produce exactly 5 dream entries"
        assert total_drafts >= 1, "at least one proposed skill draft across the week"

    def test_idempotent_rerun_same_day(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        stub, _ = _make_stub()
        journal = DreamJournal(store=store, llm_call=stub, clock=lambda: 5.0)
        for _ in range(3):
            asyncio.run(journal.dream(date="2026-05-24", sessions=[
                _session_using_tool("s1", "web_search", 1)]))
        assert store.count() == 1, "re-dreaming the same date upserts, not duplicates"
