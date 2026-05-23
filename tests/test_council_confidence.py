"""P-34: Council confidence metric — Kendall-tau + escalation policy."""
from __future__ import annotations

import pytest

from sera.council.confidence import (
    ConfidenceResult,
    _kendall_tau,
    compute_confidence,
)
from sera.council.rank import RankingResult


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _complete(order: tuple[str, ...]) -> RankingResult:
    return RankingResult(
        ranking=order,
        raw_section="",
        is_complete=True,
        missing=frozenset(),
        parse_method="numbered_bare",
    )


def _failed() -> RankingResult:
    return RankingResult(
        ranking=(),
        raw_section="",
        is_complete=False,
        missing=frozenset({"A", "B", "C"}),
        parse_method="none",
    )


# ─── _kendall_tau unit tests ───────────────────────────────────────────────────

def test_tau_identical_rankings():
    assert _kendall_tau(("A", "B", "C"), ("A", "B", "C")) == pytest.approx(1.0)


def test_tau_reversed_rankings():
    assert _kendall_tau(("A", "B", "C"), ("C", "B", "A")) == pytest.approx(-1.0)


def test_tau_one_swap():
    # (A,B,C) vs (A,C,B): only pair (B,C) is discordant
    # Pairs: (A,B): A<B in both → concordant
    #        (A,C): A<C in both → concordant
    #        (B,C): B<C in r1, C<B in r2 → discordant
    # tau = (2-1)/3 = 0.333
    assert _kendall_tau(("A", "B", "C"), ("A", "C", "B")) == pytest.approx(1 / 3)


def test_tau_partial_agreement():
    # (C,A,B) vs (A,C,B): manually verified = 1/3
    assert _kendall_tau(("C", "A", "B"), ("A", "C", "B")) == pytest.approx(1 / 3)


def test_tau_cyclic_pair():
    # (A,B,C) vs (B,C,A): C=1, D=2 → tau = -1/3
    assert _kendall_tau(("A", "B", "C"), ("B", "C", "A")) == pytest.approx(-1 / 3)


def test_tau_two_items_agree():
    assert _kendall_tau(("A", "B"), ("A", "B")) == pytest.approx(1.0)


def test_tau_two_items_disagree():
    assert _kendall_tau(("A", "B"), ("B", "A")) == pytest.approx(-1.0)


def test_tau_five_items_identical():
    r = ("A", "B", "C", "D", "E")
    assert _kendall_tau(r, r) == pytest.approx(1.0)


def test_tau_five_items_reversed():
    assert _kendall_tau(("A", "B", "C", "D", "E"), ("E", "D", "C", "B", "A")) == pytest.approx(-1.0)


# ─── compute_confidence: edge cases ───────────────────────────────────────────

def test_zero_complete_rankings():
    r = compute_confidence([_failed(), _failed()])
    assert isinstance(r, ConfidenceResult)
    assert r.tau == pytest.approx(1.0)
    assert not r.should_escalate
    assert r.pairs_evaluated == 0
    assert r.complete_rankings == 0


def test_one_complete_ranking():
    r = compute_confidence([_complete(("A", "B", "C")), _failed()])
    assert r.tau == pytest.approx(1.0)
    assert not r.should_escalate
    assert r.pairs_evaluated == 0
    assert r.complete_rankings == 1


def test_empty_rankings_list():
    r = compute_confidence([])
    assert r.tau == pytest.approx(1.0)
    assert not r.should_escalate


# ─── compute_confidence: agreement scenarios ──────────────────────────────────

def test_perfect_agreement_no_escalation():
    """All three rankers identical → tau=1.0, no escalation."""
    rankings = [_complete(("C", "A", "B"))] * 3
    r = compute_confidence(rankings)
    assert r.tau == pytest.approx(1.0)
    assert not r.should_escalate
    assert r.pairs_evaluated == 3


def test_perfect_reversal_escalates():
    """Two rankers with opposite orderings → tau=-1.0, escalate."""
    rankings = [
        _complete(("A", "B", "C")),
        _complete(("C", "B", "A")),
    ]
    r = compute_confidence(rankings)
    assert r.tau == pytest.approx(-1.0)
    assert r.should_escalate


def test_cyclic_low_agreement_escalates():
    """Cyclic disagreement (the canonical low-agreement case) triggers escalation."""
    # r1=(A,B,C), r2=(B,C,A), r3=(C,A,B) → mean tau = -1/3 ≈ -0.333 < 0.3
    rankings = [
        _complete(("A", "B", "C")),
        _complete(("B", "C", "A")),
        _complete(("C", "A", "B")),
    ]
    r = compute_confidence(rankings)
    assert r.tau == pytest.approx(-1 / 3)
    assert r.should_escalate  # -0.333 < 0.3


def test_high_agreement_no_escalation():
    """Two of three agree perfectly; one dissents on last two items → high tau."""
    rankings = [
        _complete(("C", "A", "B")),
        _complete(("C", "A", "B")),
        _complete(("C", "B", "A")),  # only last-two swapped
    ]
    r = compute_confidence(rankings)
    # tau(r1,r2)=1.0, tau(r1,r3)=1/3, tau(r2,r3)=1/3 → mean = (1+1/3+1/3)/3 = 5/9 ≈ 0.555
    assert r.tau == pytest.approx(5 / 9)
    assert not r.should_escalate


def test_tau_exactly_at_threshold_no_escalation():
    """tau == threshold should NOT escalate (strict less-than)."""
    # Craft two rankings with tau == 0.3 exactly:
    # For 3 items tau = (C-D)/3; C-D = 0.9 not integer → not achievable exactly.
    # Use threshold=1/3 and two rankings with tau=1/3 instead.
    rankings = [
        _complete(("A", "B", "C")),
        _complete(("A", "C", "B")),
    ]
    r = compute_confidence(rankings, threshold=1 / 3)
    # tau = 1/3, threshold = 1/3 → 1/3 < 1/3 is False → no escalation
    assert not r.should_escalate


def test_tau_just_below_threshold_escalates():
    rankings = [
        _complete(("A", "B", "C")),
        _complete(("A", "C", "B")),
    ]
    # tau = 1/3 ≈ 0.333; set threshold to 0.334 → should escalate
    r = compute_confidence(rankings, threshold=0.334)
    assert r.should_escalate


# ─── compute_confidence: structural correctness ───────────────────────────────

def test_result_is_confidence_result():
    r = compute_confidence([_complete(("A", "B", "C"))] * 2)
    assert isinstance(r, ConfidenceResult)


def test_threshold_recorded():
    r = compute_confidence([_complete(("A", "B", "C"))] * 2, threshold=0.5)
    assert r.threshold == pytest.approx(0.5)


def test_complete_rankings_count():
    rankings = [_complete(("A", "B", "C")), _failed(), _complete(("C", "A", "B"))]
    r = compute_confidence(rankings)
    assert r.complete_rankings == 2


def test_pairs_evaluated_two_rankings():
    r = compute_confidence([_complete(("A", "B", "C")), _complete(("C", "B", "A"))])
    assert r.pairs_evaluated == 1


def test_pairs_evaluated_three_rankings():
    r = compute_confidence([_complete(("A", "B", "C"))] * 3)
    assert r.pairs_evaluated == 3


def test_pairs_evaluated_four_rankings():
    r = compute_confidence([_complete(("A", "B", "C"))] * 4)
    assert r.pairs_evaluated == 6  # C(4,2) = 6


def test_tau_range():
    """tau must be in [-1, 1] for any valid inputs."""
    import random
    random.seed(42)
    labels = ("A", "B", "C", "D")
    for _ in range(100):
        r1 = list(labels)
        r2 = list(labels)
        random.shuffle(r1)
        random.shuffle(r2)
        tau = _kendall_tau(tuple(r1), tuple(r2))
        assert -1.0 <= tau <= 1.0


def test_custom_threshold_0():
    """threshold=0 → escalate only on negative tau."""
    rankings = [_complete(("A", "B", "C")), _complete(("A", "C", "B"))]
    # tau = 1/3 > 0 → no escalation
    r = compute_confidence(rankings, threshold=0.0)
    assert not r.should_escalate


def test_custom_threshold_1():
    """threshold=1 → always escalate unless tau is exactly 1.0."""
    rankings = [_complete(("A", "B", "C")), _complete(("A", "C", "B"))]
    r = compute_confidence(rankings, threshold=1.0)
    assert r.should_escalate
