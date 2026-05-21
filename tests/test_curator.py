"""P-23: post-session curator (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.curator.loop import should_curate, tool_call_count
from sera.memory.session import Message, Session


def _session_with_tools(tmp_path: Path, n_tool_calls: int) -> Session:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="hi"))
    s.append(
        Message(
            role="assistant",
            content="ok",
            tool_calls=[
                {"id": f"tc{i}", "type": "function",
                 "function": {"name": "file_read", "arguments": "{}"}}
                for i in range(n_tool_calls)
            ],
            finish_reason="stop",
        )
    )
    return s


# ─── Cycle 1: count + threshold ───────────────────────────────


def test_tool_call_count_sums_assistant_tool_calls(tmp_path: Path):
    s = _session_with_tools(tmp_path, n_tool_calls=3)
    assert tool_call_count(s) == 3


def test_tool_call_count_zero_for_chat_only(tmp_path: Path):
    s = Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")
    s.append(Message(role="user", content="hi"))
    s.append(Message(role="assistant", content="hello", finish_reason="stop"))
    assert tool_call_count(s) == 0


def test_should_curate_respects_threshold(tmp_path: Path):
    assert should_curate(_session_with_tools(tmp_path, 4)) is False
    assert should_curate(_session_with_tools(tmp_path, 5)) is False
    assert should_curate(_session_with_tools(tmp_path, 6)) is True


def test_should_curate_custom_threshold(tmp_path: Path):
    s = _session_with_tools(tmp_path, n_tool_calls=2)
    assert should_curate(s, threshold=1) is True


# ─── Cycle 2: Curator.review parses LLM JSON → CuratorReport ──


def test_curator_review_returns_proposals(tmp_path: Path):
    import asyncio
    import json

    from sera.curator.loop import Curator, CuratorProposal

    async def fake_llm(_prompt: str) -> str:
        return json.dumps(
            {
                "proposals": [
                    {
                        "kind": "skill_edit",
                        "payload": {"name": "caveman", "diff": "tighten preamble"},
                        "reasoning": "user repeated caveman invocation 4×",
                    },
                    {
                        "kind": "memory_note",
                        "payload": {"content": "user prefers 30-day decay"},
                        "reasoning": "stated explicitly mid-session",
                    },
                ]
            }
        )

    curator = Curator(llm_call=fake_llm)
    session = _session_with_tools(tmp_path, n_tool_calls=6)
    report = asyncio.run(curator.review(session))
    assert report.session_id == session.id
    assert len(report.proposals) == 2
    assert all(isinstance(p, CuratorProposal) for p in report.proposals)
    assert {p.kind for p in report.proposals} == {"skill_edit", "memory_note"}


def test_curator_review_tolerates_malformed_json(tmp_path: Path):
    """A garbage LLM response should produce an empty report, not crash."""
    import asyncio
    from sera.curator.loop import Curator

    async def bad_llm(_prompt: str) -> str:
        return "definitely not json {"

    curator = Curator(llm_call=bad_llm)
    session = _session_with_tools(tmp_path, n_tool_calls=6)
    report = asyncio.run(curator.review(session))
    assert report.proposals == ()
    assert report.error is not None
    assert "json" in report.error.lower()


def test_curator_drops_unknown_proposal_kinds(tmp_path: Path):
    import asyncio
    import json
    from sera.curator.loop import Curator

    async def llm(_prompt: str) -> str:
        return json.dumps(
            {
                "proposals": [
                    {"kind": "skill_edit", "payload": {}, "reasoning": "ok"},
                    {"kind": "frobnicate", "payload": {}, "reasoning": "no"},
                ]
            }
        )

    curator = Curator(llm_call=llm)
    session = _session_with_tools(tmp_path, n_tool_calls=6)
    report = asyncio.run(curator.review(session))
    kinds = [p.kind for p in report.proposals]
    assert kinds == ["skill_edit"]


def test_curator_review_passes_trace_to_llm(tmp_path: Path):
    import asyncio
    import json
    from sera.curator.loop import Curator

    captured: dict[str, str] = {}

    async def llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps({"proposals": []})

    curator = Curator(llm_call=llm)
    session = _session_with_tools(tmp_path, n_tool_calls=6)
    asyncio.run(curator.review(session))
    # The prompt must mention the tool name so the LLM has context to propose
    # against. We don't lock the whole format — just the existence signal.
    assert "file_read" in captured["prompt"]


# ─── Cycle 3: CuratorStore persists ───────────────────────────


def test_curator_store_records_and_reads(tmp_path: Path):
    from sera.curator.loop import (
        CuratorProposal,
        CuratorReport,
        CuratorStore,
    )

    store = CuratorStore(db_path=tmp_path / "curator.db")
    report = CuratorReport(
        session_id="sess1",
        proposals=(
            CuratorProposal(kind="skill_edit",
                            payload={"name": "caveman"},
                            reasoning="frequent invocation"),
            CuratorProposal(kind="memory_note",
                            payload={"content": "user prefers 30-day decay"},
                            reasoning="stated"),
        ),
        started_at=100.0,
        finished_at=101.5,
    )
    store.record(report)

    recent = store.recent_reports(limit=5)
    assert len(recent) == 1
    assert recent[0].session_id == "sess1"
    assert recent[0].finished_at == pytest.approx(101.5)
    assert {p.kind for p in recent[0].proposals} == {"skill_edit", "memory_note"}


def test_curator_store_persists_error_field(tmp_path: Path):
    from sera.curator.loop import CuratorReport, CuratorStore

    store = CuratorStore(db_path=tmp_path / "curator.db")
    store.record(CuratorReport(
        session_id="sess2",
        proposals=(),
        started_at=200.0,
        finished_at=200.1,
        error="json parse failed",
    ))
    recent = store.recent_reports(limit=5)
    assert recent[0].error == "json parse failed"


def test_curator_store_orders_by_recency(tmp_path: Path):
    from sera.curator.loop import CuratorReport, CuratorStore

    store = CuratorStore(db_path=tmp_path / "curator.db")
    store.record(CuratorReport(session_id="old", started_at=100, finished_at=101))
    store.record(CuratorReport(session_id="new", started_at=200, finished_at=201))
    recent = store.recent_reports(limit=5)
    assert [r.session_id for r in recent] == ["new", "old"]


def test_curator_store_handles_empty(tmp_path: Path):
    from sera.curator.loop import CuratorStore

    store = CuratorStore(db_path=tmp_path / "curator.db")
    assert store.recent_reports(limit=5) == []


# ─── Cycle 4: CuratorQueue background ────────────────────────


def test_queue_processes_session_in_background(tmp_path: Path):
    """Phase verification: synthetic 10-tool session → log entry within 60s."""
    import json
    from sera.curator.loop import Curator, CuratorQueue, CuratorStore

    async def llm(_prompt: str) -> str:
        return json.dumps(
            {
                "proposals": [
                    {"kind": "skill_edit",
                     "payload": {"name": "auto"},
                     "reasoning": "synthetic"}
                ]
            }
        )

    store = CuratorStore(db_path=tmp_path / "curator.db")
    queue = CuratorQueue(store=store, curator_factory=lambda: Curator(llm_call=llm))
    queue.start()
    try:
        session = _session_with_tools(tmp_path, n_tool_calls=10)
        queue.enqueue(session)
        assert queue.wait_idle(timeout=5.0), "curator queue never drained"
    finally:
        queue.stop(timeout=2.0)
    reports = store.recent_reports(limit=5)
    assert len(reports) == 1
    assert reports[0].session_id == session.id
    assert reports[0].proposals
    assert reports[0].proposals[0].kind == "skill_edit"


def test_queue_skips_low_tool_count(tmp_path: Path):
    """Sessions below threshold should not produce reports."""
    from sera.curator.loop import Curator, CuratorQueue, CuratorStore

    calls: list[str] = []

    async def llm(_prompt: str) -> str:
        calls.append("called")
        return "{}"

    store = CuratorStore(db_path=tmp_path / "curator.db")
    queue = CuratorQueue(store=store, curator_factory=lambda: Curator(llm_call=llm))
    queue.start()
    try:
        session = _session_with_tools(tmp_path, n_tool_calls=2)
        queue.enqueue(session)
        queue.wait_idle(timeout=1.0)
    finally:
        queue.stop(timeout=2.0)
    assert calls == []
    assert store.recent_reports(limit=5) == []


def test_queue_swallows_curator_exception(tmp_path: Path):
    """A curator crash must not kill the worker thread; report still logs."""
    from sera.curator.loop import Curator, CuratorQueue, CuratorStore

    async def boom(_prompt: str) -> str:
        raise RuntimeError("simulated failure")

    store = CuratorStore(db_path=tmp_path / "curator.db")
    queue = CuratorQueue(store=store, curator_factory=lambda: Curator(llm_call=boom))
    queue.start()
    try:
        session = _session_with_tools(tmp_path, n_tool_calls=6)
        queue.enqueue(session)
        assert queue.wait_idle(timeout=2.0)
    finally:
        queue.stop(timeout=2.0)
    reports = store.recent_reports(limit=5)
    assert len(reports) == 1
    assert reports[0].error is not None
    assert "simulated failure" in reports[0].error


def test_queue_double_stop_is_idempotent(tmp_path: Path):
    from sera.curator.loop import Curator, CuratorQueue, CuratorStore

    async def llm(_p: str) -> str:
        return "{}"

    store = CuratorStore(db_path=tmp_path / "curator.db")
    q = CuratorQueue(store=store, curator_factory=lambda: Curator(llm_call=llm))
    q.stop()   # never started — must not raise
    q.start()
    q.stop()
    q.stop()


# ─── Cycle 5: sera curator log CLI ─────────────────────────────


def test_cli_curator_log_prints_recent(tmp_path: Path):
    from click.testing import CliRunner

    from sera.cli.main import main
    from sera.curator.loop import CuratorProposal, CuratorReport, CuratorStore

    db = tmp_path / "curator.db"
    store = CuratorStore(db_path=db)
    store.record(
        CuratorReport(
            session_id="abc123",
            proposals=(
                CuratorProposal(
                    kind="skill_edit",
                    payload={"name": "egoist"},
                    reasoning="user repeated /egoist",
                ),
            ),
            started_at=100.0,
            finished_at=101.0,
        )
    )

    runner = CliRunner()
    result = runner.invoke(main, ["curator", "log", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    assert "skill_edit" in result.output


def test_cli_curator_log_empty_message(tmp_path: Path):
    from click.testing import CliRunner

    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(
        main, ["curator", "log", "--db", str(tmp_path / "curator.db")]
    )
    assert result.exit_code == 0
    assert "no curator" in result.output.lower() or "empty" in result.output.lower()
