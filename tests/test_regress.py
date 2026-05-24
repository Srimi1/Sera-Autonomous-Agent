"""Tests for sera.eval.regress — P-80 Hill-climb regression suite.

Phase verification: bad LoRA never promotes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.eval.regress import PromotionResult, RegressionGate


# ---------------------------------------------------------------------------
# Stub GainTracker
# ---------------------------------------------------------------------------

class _StubTracker:
    def __init__(self, scores: list[tuple[str, float]]) -> None:
        self._scores = scores

    def scores(self) -> list[tuple[str, float]]:
        return self._scores


def _gate(scores: list[tuple[str, float]] | None = None) -> RegressionGate:
    tracker = _StubTracker(scores or []) if scores is not None else None
    return RegressionGate(gain_tracker=tracker)


# ---------------------------------------------------------------------------
# First-night (no baseline)
# ---------------------------------------------------------------------------

class TestFirstNight:
    def test_no_tracker_always_promotes(self) -> None:
        gate = RegressionGate(gain_tracker=None)
        result = gate.evaluate(0.75)
        assert result.promoted
        assert result.baseline_score is None
        assert result.delta_pp is None

    def test_empty_history_promotes(self) -> None:
        gate = _gate(scores=[])
        result = gate.evaluate(0.80)
        assert result.promoted

    def test_first_night_reason_mentions_baseline(self) -> None:
        gate = _gate(scores=[])
        result = gate.evaluate(0.80)
        assert "first" in result.reason or "no baseline" in result.reason


# ---------------------------------------------------------------------------
# Improvement → promote
# ---------------------------------------------------------------------------

class TestPromotion:
    def test_strictly_better_promotes(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.83)
        assert result.promoted
        assert result.delta_pp == pytest.approx(3.0)

    def test_delta_pp_positive_on_improvement(self) -> None:
        gate = _gate([("d1", 0.70)])
        result = gate.evaluate(0.75)
        assert result.delta_pp > 0

    def test_baseline_is_last_score(self) -> None:
        gate = _gate([("d1", 0.70), ("d2", 0.80)])
        result = gate.evaluate(0.85)
        assert result.baseline_score == pytest.approx(0.80)

    def test_promoted_result_shape(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.82)
        assert result.promoted
        assert result.candidate_score == pytest.approx(0.82)
        assert result.baseline_score == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# Regression → block
# ---------------------------------------------------------------------------

class TestRegression:
    def test_worse_is_blocked(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.75)
        assert not result.promoted

    def test_equal_is_blocked(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.80)
        assert not result.promoted

    def test_delta_pp_negative_on_regression(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.70)
        assert result.delta_pp == pytest.approx(-10.0)

    def test_reason_mentions_regression(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.78)
        assert "regression" in result.reason or "block" in result.reason.lower()

    def test_improvement_pp_negative(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.75)
        assert result.improvement_pp < 0

    def test_blocked_result_shape(self) -> None:
        gate = _gate([("d1", 0.80)])
        result = gate.evaluate(0.70)
        assert not result.promoted
        assert result.candidate_score == pytest.approx(0.70)
        assert result.baseline_score == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_score_above_one_raises(self) -> None:
        gate = _gate([])
        with pytest.raises(ValueError):
            gate.evaluate(1.5)

    def test_score_below_zero_raises(self) -> None:
        gate = _gate([])
        with pytest.raises(ValueError):
            gate.evaluate(-0.1)

    def test_zero_is_valid(self) -> None:
        gate = _gate([])
        result = gate.evaluate(0.0)
        assert result.promoted   # first night

    def test_one_is_valid(self) -> None:
        gate = _gate([("d1", 0.90)])
        result = gate.evaluate(1.0)
        assert result.promoted


# ---------------------------------------------------------------------------
# THE VERIFICATION: bad LoRA never promotes
# ---------------------------------------------------------------------------

class TestBadLoRaNeverPromotes:
    def test_bad_lora_blocked(self) -> None:
        """Phase gate: adapter with lower eval score must never promote."""
        # Baseline: last night scored 0.82
        gate = _gate([
            ("2026-05-20", 0.78),
            ("2026-05-21", 0.80),
            ("2026-05-22", 0.82),
        ])

        # Bad LoRA — scores worse than last night
        bad_scores = [0.60, 0.75, 0.80, 0.819]
        for score in bad_scores:
            result = gate.evaluate(score, adapter_dir=Path("/tmp/bad_adapter"))
            assert not result.promoted, (
                f"bad LoRA with score={score} was promoted over baseline=0.82"
            )

    def test_good_lora_promotes(self) -> None:
        """Corollary: adapter that beats baseline must promote."""
        gate = _gate([("d1", 0.82)])
        result = gate.evaluate(0.83)
        assert result.promoted

    def test_multiple_nights_only_best_gate_applies(self) -> None:
        """Baseline is always the most recent night, not the best ever."""
        gate = _gate([
            ("d1", 0.90),   # historical peak
            ("d2", 0.80),   # last night — this is the baseline
        ])
        # 0.85 beats last night (0.80) but not the historical peak (0.90)
        result = gate.evaluate(0.85)
        assert result.promoted, (
            "should promote vs last night's baseline, not historical peak"
        )
