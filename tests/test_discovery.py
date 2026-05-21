"""P-30: discovery agent — proactive skill proposal (TDD)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from sera.curator.discovery import (
    MIN_PATTERN_FREQUENCY,
    DiscoveryAgent,
    DiscoveryProposal,
    DiscoveryRun,
    run_discovery,
    tool_pattern_counts,
)
from sera.memory.session import Message


# ─── Helpers ──────────────────────────────────────────────────────


def _make_session(tool_names: list[str], user_msg: str = "do the thing") -> Any:
    """Minimal fake session with tool calls and one user message."""

    @dataclass
    class FakeSession:
        id: str
        messages: list[Message] = field(default_factory=list)

    msgs: list[Message] = [Message(role="user", content=user_msg)]
    for name in tool_names:
        msgs.append(
            Message(
                role="assistant",
                content=None,
                tool_calls=[{"function": {"name": name, "arguments": "{}"}}],
            )
        )
    s = FakeSession(id=f"sess-{user_msg[:8]}-{len(tool_names)}")
    s.messages = msgs
    return s


# ─── Cycle 1: tool_pattern_counts heuristic ───────────────────────


def test_tool_pattern_counts_sums_across_sessions():
    sessions = [
        _make_session(["search", "search"]),
        _make_session(["search", "summarise"]),
        _make_session(["summarise", "summarise"]),
    ]
    counts = tool_pattern_counts(sessions)
    assert counts["search"] == 3
    assert counts["summarise"] == 3


def test_tool_pattern_counts_empty_sessions():
    assert tool_pattern_counts([]) == {}


def test_tool_pattern_counts_no_tool_calls():
    s = _make_session([])
    assert tool_pattern_counts([s]) == {}


def test_min_pattern_frequency_constant_exists():
    assert isinstance(MIN_PATTERN_FREQUENCY, int)
    assert MIN_PATTERN_FREQUENCY >= 2


# ─── Cycle 2: DiscoveryAgent.run with injected LLM ───────────────


async def _fake_llm(prompt: str) -> str:
    """Stub LLM — always proposes one skill for 'search'."""
    return (
        '{"proposals": ['
        '{"trigger": "/search", "name": "quick_search", '
        '"description": "Run a quick web search", '
        '"body_hint": "Search the web for {{query}}", '
        '"reasoning": "user called search 5 times across sessions"}'
        ']}'
    )


def test_discovery_agent_returns_proposals():
    sessions = [_make_session(["search"] * 3)]
    agent = DiscoveryAgent(llm_call=_fake_llm)
    run = asyncio.run(agent.run(sessions, known_triggers=set()))
    assert isinstance(run, DiscoveryRun)
    assert len(run.proposals) >= 1
    assert run.proposals[0].trigger == "/search"


def test_discovery_agent_skips_known_triggers():
    sessions = [_make_session(["search"] * 3)]
    agent = DiscoveryAgent(llm_call=_fake_llm)
    run = asyncio.run(agent.run(sessions, known_triggers={"/search"}))
    assert len(run.proposals) == 0


def test_discovery_agent_no_pattern_skips_llm():
    """Below MIN_PATTERN_FREQUENCY → LLM never called, empty run."""
    called = []

    async def spy_llm(prompt: str) -> str:
        called.append(prompt)
        return '{"proposals": []}'

    # Only 1 call to 'search' — below threshold.
    sessions = [_make_session(["search"])]
    agent = DiscoveryAgent(llm_call=spy_llm)
    run = asyncio.run(agent.run(sessions, known_triggers=set()))
    assert called == []  # LLM not invoked
    assert run.proposals == ()


def test_discovery_agent_llm_error_returns_empty_run():
    async def bad_llm(prompt: str) -> str:
        raise RuntimeError("LLM down")

    sessions = [_make_session(["search"] * MIN_PATTERN_FREQUENCY)]
    agent = DiscoveryAgent(llm_call=bad_llm)
    run = asyncio.run(agent.run(sessions, known_triggers=set()))
    assert run.proposals == ()
    assert run.error is not None


def test_discovery_run_records_sessions_scanned():
    sessions = [_make_session(["search"] * 3) for _ in range(4)]
    agent = DiscoveryAgent(llm_call=_fake_llm)
    run = asyncio.run(agent.run(sessions, known_triggers=set()))
    assert run.sessions_scanned == 4


# ─── Cycle 3: 5-day synthetic usage → ≥1 proposal ────────────────


def test_five_days_synthetic_usage_yields_proposal():
    """Verification clause: 5 sessions, same tool 3+ times/session → proposal."""
    sessions = [_make_session(["web_search"] * 3, user_msg=f"day {i}") for i in range(5)]
    agent = DiscoveryAgent(llm_call=_fake_llm)
    run = asyncio.run(agent.run(sessions, known_triggers=set()))
    assert len(run.proposals) >= 1


def test_run_discovery_convenience_wrapper():
    """run_discovery is the one-liner daily pass entry point."""
    sessions = [_make_session(["search"] * 3)]
    run = asyncio.run(run_discovery(sessions, known_triggers=set(), llm_call=_fake_llm))
    assert isinstance(run, DiscoveryRun)


def test_discovery_proposal_fields():
    p = DiscoveryProposal(
        trigger="/search",
        name="quick_search",
        description="search the web",
        body_hint="Search for {{query}}",
        source_session_ids=["s1", "s2"],
        reasoning="called 5 times",
    )
    assert p.trigger == "/search"
    assert p.frequency == 0  # default


# ─── Cycle 4: CLI sera curator discover ───────────────────────────


def test_cli_curator_discover_no_sessions(tmp_path):
    from click.testing import CliRunner
    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["curator", "discover",
         "--curator-db", str(tmp_path / "curator.db"),
         "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "no sessions" in result.output.lower() or "0" in result.output


def test_cli_curator_discover_dry_run_shows_proposals(tmp_path):
    """With pre-seeded curator store, discover surfaces proposals."""
    from click.testing import CliRunner
    from sera.cli.main import main
    from sera.curator.loop import CuratorReport, CuratorProposal, CuratorStore

    store = CuratorStore(db_path=tmp_path / "curator.db")
    # Seed 5 reports with 'search' in payload to simulate usage.
    for i in range(5):
        store.record(CuratorReport(
            session_id=f"s{i}",
            proposals=(
                CuratorProposal(
                    kind="tool_hint",
                    payload={"tool": "search"},
                    reasoning="user searched",
                ),
            ),
            started_at=float(i),
            finished_at=float(i) + 1.0,
        ))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["curator", "discover",
         "--curator-db", str(tmp_path / "curator.db"),
         "--dry-run"],
    )
    assert result.exit_code == 0, result.output
