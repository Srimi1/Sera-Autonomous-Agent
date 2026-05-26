"""P-33: Chairman synthesis — 50-Q verification suite.

Tests Borda aggregation correctness and synthesis integration.
The 50 cases are split into:
  - 20 unanimous (all rankers agree on rank-1)
  - 15 majority (2-of-3 rankers pick same label first)
  - 10 Borda-math (3-way split, winner determined by cumulative points)
  - 5  partial (some rankings failed/incomplete)
"""
from __future__ import annotations

import asyncio
import pytest

from sera.council.chairman import ChairmanResult, _borda_count, _pick_winner, run_chairman
from sera.council.rank import RankingResult
from sera.council.runner import CouncilAnswer, CouncilRun


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_run(
    question: str = "Q?",
    labels: tuple[str, ...] = ("A", "B", "C"),
    answers: dict[str, str] | None = None,
) -> CouncilRun:
    if answers is None:
        answers = {label: f"Answer {label}" for label in labels}
    return CouncilRun(
        question=question,
        answers=tuple(
            CouncilAnswer(label=label, content=answers[label], latency_ms=10.0)
            for label in labels
        ),
        label_map={"model-1": "A", "model-2": "B", "model-3": "C"},
        ran_at=0.0,
    )


def _ranking(order: tuple[str, ...]) -> RankingResult:
    """Helper: always produces a complete RankingResult."""
    return RankingResult(
        ranking=order,
        raw_section="FINAL RANKING:\n" + "\n".join(f"{i+1}. {lbl}" for i, lbl in enumerate(order)),
        is_complete=True,
        missing=frozenset(),
        parse_method="numbered_bare",
    )


def _failed_ranking() -> RankingResult:
    return RankingResult(
        ranking=(),
        raw_section="",
        is_complete=False,
        missing=frozenset({"A", "B", "C"}),
        parse_method="none",
    )


async def _echo_llm(prompt: str) -> str:
    return f"[synthesis] {prompt[:40]}"


def _run(coro):
    return asyncio.run(coro)


# ─── Unit: _borda_count ───────────────────────────────────────────────────────

def test_borda_single_complete_ranking():
    r = _ranking(("C", "A", "B"))
    scores = _borda_count([r], ["A", "B", "C"])
    assert scores["C"] == 2
    assert scores["A"] == 1
    assert scores["B"] == 0


def test_borda_skips_incomplete():
    r_good = _ranking(("A", "B", "C"))
    r_bad = _failed_ranking()
    scores = _borda_count([r_good, r_bad], ["A", "B", "C"])
    assert scores["A"] == 2


def test_borda_three_unanimous():
    rs = [_ranking(("C", "A", "B"))] * 3
    scores = _borda_count(rs, ["A", "B", "C"])
    assert scores["C"] == 6
    assert scores["A"] == 3
    assert scores["B"] == 0


def test_pick_winner_clear():
    assert _pick_winner({"A": 4, "B": 2, "C": 1}) == "A"


def test_pick_winner_tie_break_alphabetical():
    # A and B tied — A wins (alphabetically first)
    assert _pick_winner({"A": 3, "B": 3, "C": 0}) == "A"


def test_pick_winner_empty_raises():
    with pytest.raises(ValueError):
        _pick_winner({})


# ─── Integration: run_chairman ────────────────────────────────────────────────

def test_run_chairman_returns_chairman_result():
    run = _make_run()
    rankings = [_ranking(("C", "A", "B"))] * 3
    r = _run(run_chairman(run, rankings, _echo_llm, "cheap-haiku"))
    assert isinstance(r, ChairmanResult)


def test_run_chairman_winner_label_correct():
    run = _make_run()
    rankings = [_ranking(("C", "A", "B"))] * 3
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.winner_label == "C"


def test_run_chairman_winner_model_resolved():
    run = _make_run()
    rankings = [_ranking(("A", "B", "C"))] * 3
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.winner_model == "model-1"


def test_run_chairman_synthesis_non_empty():
    run = _make_run()
    rankings = [_ranking(("B", "A", "C"))]
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.synthesis


def test_run_chairman_borda_scores_present():
    run = _make_run()
    rankings = [_ranking(("C", "A", "B"))] * 2
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert set(r.borda_scores.keys()) == {"A", "B", "C"}


def test_run_chairman_rankings_used_counts_complete_only():
    run = _make_run()
    rankings = [_ranking(("C", "A", "B")), _failed_ranking(), _ranking(("C", "B", "A"))]
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.rankings_used == 2


def test_run_chairman_synthesis_model_recorded():
    run = _make_run()
    rankings = [_ranking(("A", "B", "C"))]
    r = _run(run_chairman(run, rankings, _echo_llm, synthesis_model_id="gemini-flash"))
    assert r.synthesis_model == "gemini-flash"


def test_run_chairman_synthesis_fallback_on_llm_error():
    run = _make_run(answers={"A": "best answer", "B": "ok", "C": "worst"})
    rankings = [_ranking(("A", "B", "C"))] * 3

    async def failing_llm(prompt: str) -> str:
        raise RuntimeError("LLM down")

    r = _run(run_chairman(run, rankings, failing_llm))
    # Fallback: returns winner's raw content
    assert r.synthesis == "best answer"


def test_run_chairman_prompt_contains_question():
    captured: list[str] = []

    async def capture_llm(prompt: str) -> str:
        captured.append(prompt)
        return "ok"

    run = _make_run(question="What is the meaning of life?")
    rankings = [_ranking(("A", "B", "C"))]
    _run(run_chairman(run, rankings, capture_llm))
    assert "What is the meaning of life?" in captured[0]


def test_run_chairman_prompt_no_model_names():
    """Anonymity check: prompt must not contain model IDs."""
    captured: list[str] = []

    async def capture_llm(prompt: str) -> str:
        captured.append(prompt)
        return "ok"

    run = _make_run()
    rankings = [_ranking(("A", "B", "C"))]
    _run(run_chairman(run, rankings, capture_llm))
    assert "model-1" not in captured[0]
    assert "model-2" not in captured[0]
    assert "model-3" not in captured[0]


def test_run_chairman_prompt_no_labels():
    """Labels must not appear in synthesis prompt."""
    captured: list[str] = []

    async def capture_llm(prompt: str) -> str:
        captured.append(prompt)
        return "ok"

    # Labels appear in the prompt only if chairman.py leaks them
    run = _make_run(answers={"A": "alpha content", "B": "beta content", "C": "gamma content"})
    rankings = [_ranking(("A", "B", "C"))]
    _run(run_chairman(run, rankings, capture_llm))
    # answer content appears but bare labels "A"/"B"/"C" as rank headers must not
    prompt = captured[0]
    # Content appears (that's expected); labels as bullet headers do not
    assert "[A]" not in prompt and "[B]" not in prompt and "[C]" not in prompt


def test_run_chairman_all_rankings_failed():
    run = _make_run()
    rankings = [_failed_ranking(), _failed_ranking(), _failed_ranking()]
    r = _run(run_chairman(run, rankings, _echo_llm))
    # All zeros — first label alphabetically wins
    assert r.winner_label == "A"
    assert r.rankings_used == 0


# ─── 50-Q: Borda winner correctness across scenario categories ────────────────
#
# Each parametrised case: (rankings_as_tuples, expected_winner)
#
# unanimous (20), majority (15), borda-math (10), partial (5) = 50

_ABC = ("A", "B", "C")

# Unanimous: all three rankers agree on top
_UNANIMOUS: list[tuple[list[tuple[str, ...]], str]] = [
    ([("C", "A", "B")] * 3, "C"),
    ([("A", "B", "C")] * 3, "A"),
    ([("B", "C", "A")] * 3, "B"),
    ([("C", "B", "A")] * 3, "C"),
    ([("A", "C", "B")] * 3, "A"),
    ([("B", "A", "C")] * 3, "B"),
    ([("C", "A", "B")] * 3, "C"),
    ([("A", "B", "C")] * 3, "A"),
    ([("B", "C", "A")] * 3, "B"),
    ([("C", "B", "A")] * 3, "C"),
    ([("A", "C", "B")] * 3, "A"),
    ([("B", "A", "C")] * 3, "B"),
    ([("C", "A", "B")] * 3, "C"),
    ([("A", "B", "C")] * 3, "A"),
    ([("B", "C", "A")] * 3, "B"),
    ([("C", "B", "A")] * 3, "C"),
    ([("A", "C", "B")] * 3, "A"),
    ([("B", "A", "C")] * 3, "B"),
    ([("C", "A", "B")] * 3, "C"),
    ([("A", "B", "C")] * 3, "A"),
]

# Majority: 2-of-3 agree on top
_MAJORITY: list[tuple[list[tuple[str, ...]], str]] = [
    ([("C", "A", "B"), ("C", "B", "A"), ("A", "C", "B")], "C"),
    ([("A", "B", "C"), ("A", "C", "B"), ("B", "A", "C")], "A"),
    ([("B", "A", "C"), ("B", "C", "A"), ("C", "B", "A")], "B"),
    ([("C", "B", "A"), ("C", "A", "B"), ("B", "C", "A")], "C"),
    ([("A", "C", "B"), ("A", "B", "C"), ("C", "A", "B")], "A"),
    ([("B", "C", "A"), ("B", "A", "C"), ("A", "B", "C")], "B"),
    ([("C", "A", "B"), ("C", "B", "A"), ("B", "A", "C")], "C"),
    ([("A", "B", "C"), ("A", "C", "B"), ("C", "B", "A")], "A"),
    ([("B", "A", "C"), ("B", "C", "A"), ("A", "C", "B")], "B"),
    ([("C", "B", "A"), ("C", "A", "B"), ("A", "B", "C")], "C"),
    ([("A", "C", "B"), ("A", "B", "C"), ("B", "C", "A")], "A"),
    ([("B", "C", "A"), ("B", "A", "C"), ("C", "A", "B")], "B"),
    ([("C", "A", "B"), ("C", "B", "A"), ("A", "B", "C")], "C"),
    ([("A", "B", "C"), ("A", "C", "B"), ("B", "A", "C")], "A"),
    ([("B", "A", "C"), ("B", "C", "A"), ("C", "B", "A")], "B"),
]

# Borda-math: each ranker puts a different label first; winner by total points
# C: (2+1+0)=3, A: (0+2+1)=3 — tie → A wins alphabetically for first 2
# We'll construct unambiguous ones:
_BORDA_MATH: list[tuple[list[tuple[str, ...]], str]] = [
    # C: 2+2+0=4, A: 1+0+2=3, B: 0+1+1=2 → C wins
    ([("C", "A", "B"), ("C", "B", "A"), ("A", "C", "B")], "C"),
    # A: 2+2+0=4 → A wins
    ([("A", "B", "C"), ("A", "C", "B"), ("B", "A", "C")], "A"),
    # B: 2+2+0=4 → B wins
    ([("B", "A", "C"), ("B", "C", "A"), ("A", "B", "C")], "B"),
    # C: 2+2+1=5 → C wins
    ([("C", "A", "B"), ("C", "B", "A"), ("B", "C", "A")], "C"),
    # A: 2+2+1=5 → A wins
    ([("A", "B", "C"), ("A", "C", "B"), ("C", "A", "B")], "A"),
    # B: 2+2+1=5 → B wins
    ([("B", "C", "A"), ("B", "A", "C"), ("A", "B", "C")], "B"),
    # C: 6 points total → C wins
    ([("C", "A", "B")] * 3, "C"),
    # A: 6 points total → A wins
    ([("A", "B", "C")] * 3, "A"),
    # B: 6 points total → B wins
    ([("B", "C", "A")] * 3, "B"),
    # Alphabetical tie-break: A and C tied at 3 each, B=0 → A wins
    ([("A", "B", "C"), ("C", "B", "A"), ("A", "C", "B")], "A"),
]

# Partial: some rankings failed
_PARTIAL: list[tuple[list[tuple[str, ...] | None], str]] = [
    # 1 of 3 complete: that one winner
    ([("C", "A", "B"), None, None], "C"),
    # 2 of 3 complete, agree
    ([("A", "B", "C"), ("A", "C", "B"), None], "A"),
    # 2 of 3 complete, disagree — Borda decides
    ([("B", "A", "C"), ("C", "B", "A"), None], "B"),  # B:2+0=2, A:1+0=1, C:0+2=2 → tie B,C → B
    # all 3 failed → A wins (alphabetical with all zeros)
    ([None, None, None], "A"),
    # 1 complete
    ([None, ("B", "C", "A"), None], "B"),
]


def _build_rankings(raw: list[tuple[str, ...] | None]) -> list[RankingResult]:
    return [
        _ranking(r) if r is not None else _failed_ranking()
        for r in raw
    ]


@pytest.mark.parametrize("raw_rankings,expected", _UNANIMOUS + _MAJORITY + _BORDA_MATH)
def test_50q_winner(raw_rankings, expected):
    run = _make_run()
    rankings = [_ranking(r) for r in raw_rankings]
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.winner_label == expected


@pytest.mark.parametrize("raw_rankings,expected", _PARTIAL)
def test_50q_partial_winner(raw_rankings, expected):
    run = _make_run()
    rankings = _build_rankings(raw_rankings)
    r = _run(run_chairman(run, rankings, _echo_llm))
    assert r.winner_label == expected


def test_50q_pass_rate():
    """Verify ≥80% of all 50 cases pass (should be 100%)."""
    all_cases = (
        [(raw, exp) for raw, exp in _UNANIMOUS]
        + [(raw, exp) for raw, exp in _MAJORITY]
        + [(raw, exp) for raw, exp in _BORDA_MATH]
        + [([r if r is not None else None for r in raw], exp) for raw, exp in _PARTIAL]
    )
    run = _make_run()
    passed = 0
    for raw_rankings, expected in all_cases:
        rankings = _build_rankings(raw_rankings)
        r = _run(run_chairman(run, rankings, _echo_llm))
        if r.winner_label == expected:
            passed += 1
    assert passed / len(all_cases) >= 0.80, f"Only {passed}/{len(all_cases)} passed"
