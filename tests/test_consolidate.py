"""Tests for sera.dream.consolidate — P-77 Cross-session consolidation.

Phase verification: 3 contradictions in test → 1 reconciliation prompt.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from sera.dream.consolidate import (
    ConsolidationEngine,
    ConsolidationResult,
    Contradiction,
    _build_reconciliation_prompt,
)


# ---------------------------------------------------------------------------
# Stub LLM
# ---------------------------------------------------------------------------

def _stub_with(contradictions: list[dict]) -> object:
    async def stub(prompt: str) -> str:
        return json.dumps({"contradictions": contradictions})
    return stub


def _failing_stub(prompt: str):
    async def stub(p: str):
        raise RuntimeError("model down")
    return stub()


# ---------------------------------------------------------------------------
# _build_reconciliation_prompt
# ---------------------------------------------------------------------------

class TestBuildReconciliationPrompt:
    def test_mentions_count(self) -> None:
        cs = [Contradiction("a", "b", "conflict")]
        p = _build_reconciliation_prompt(cs)
        assert "1 contradiction" in p

    def test_plural(self) -> None:
        cs = [Contradiction("a", "b", "c"), Contradiction("d", "e", "f")]
        p = _build_reconciliation_prompt(cs)
        assert "2 contradictions" in p

    def test_includes_both_facts(self) -> None:
        cs = [Contradiction("user is in NYC", "user is in London", "location conflict")]
        p = _build_reconciliation_prompt(cs)
        assert "NYC" in p
        assert "London" in p

    def test_ends_with_question(self) -> None:
        cs = [Contradiction("a", "b", "r")]
        p = _build_reconciliation_prompt(cs)
        assert p.strip().endswith("?")

    def test_numbered_items(self) -> None:
        cs = [Contradiction("a", "b", "r1"), Contradiction("c", "d", "r2")]
        p = _build_reconciliation_prompt(cs)
        assert "1." in p
        assert "2." in p


# ---------------------------------------------------------------------------
# ConsolidationEngine
# ---------------------------------------------------------------------------

class TestConsolidationEngine:
    def run(self, stub, facts):
        engine = ConsolidationEngine(llm_call=stub)
        return asyncio.run(engine.consolidate(facts))

    def test_no_contradictions_returned(self) -> None:
        stub = _stub_with([])
        result = self.run(stub, ["user is in NYC", "user likes Python"])
        assert not result.has_conflicts
        assert result.reconciliation_prompt == ""

    def test_single_contradiction_returned(self) -> None:
        stub = _stub_with([{"a": "user is in NYC", "b": "user is in London",
                            "reason": "location conflict"}])
        result = self.run(stub, ["user is in NYC", "user is in London"])
        assert result.has_conflicts
        assert len(result.contradictions) == 1
        assert result.contradictions[0].a == "user is in NYC"

    def test_three_contradictions_one_prompt(self) -> None:
        """Phase gate: 3 contradictions → single reconciliation prompt."""
        stub = _stub_with([
            {"a": "user is in NYC",    "b": "user is in London", "reason": "location"},
            {"a": "user prefers vim",  "b": "user prefers VSCode", "reason": "editor"},
            {"a": "project uses Python", "b": "project uses Go",  "reason": "language"},
        ])
        facts = [
            "user is in NYC", "user is in London",
            "user prefers vim", "user prefers VSCode",
            "project uses Python", "project uses Go",
        ]
        result = self.run(stub, facts)
        assert len(result.contradictions) == 3
        assert result.reconciliation_prompt, "must produce exactly one prompt"
        assert "3 contradictions" in result.reconciliation_prompt
        # Verify all contradictions are in the single prompt
        assert "NYC" in result.reconciliation_prompt
        assert "vim" in result.reconciliation_prompt
        assert "Python" in result.reconciliation_prompt

    def test_too_few_facts_skips_llm(self) -> None:
        called = []
        async def spy(p):
            called.append(p)
            return json.dumps({"contradictions": []})

        engine = ConsolidationEngine(llm_call=spy)
        asyncio.run(engine.consolidate(["single fact"]))
        assert not called, "LLM must not be called for fewer than 2 facts"

    def test_llm_failure_is_soft(self) -> None:
        async def broken(p: str):
            raise RuntimeError("network error")

        engine = ConsolidationEngine(llm_call=broken)
        result = asyncio.run(engine.consolidate(["fact a", "fact b"]))
        assert result.error is not None
        assert not result.has_conflicts

    def test_malformed_item_skipped(self) -> None:
        stub = _stub_with([
            {"a": "",    "b": "b", "reason": "r"},   # empty a
            {"a": "a",   "b": "",  "reason": "r"},   # empty b
            {"a": "ok1", "b": "ok2", "reason": "real conflict"},
        ])
        result = self.run(stub, ["ok1", "ok2"])
        assert len(result.contradictions) == 1

    def test_non_dict_item_skipped(self) -> None:
        async def stub(p: str) -> str:
            return json.dumps({"contradictions": ["not a dict", {"a": "x", "b": "y", "reason": "r"}]})

        engine = ConsolidationEngine(llm_call=stub)
        result = asyncio.run(engine.consolidate(["x", "y"]))
        assert len(result.contradictions) == 1
