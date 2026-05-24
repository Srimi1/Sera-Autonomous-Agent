"""Hill-climb regression suite — P-80.

OUTCLASS: No LoRA adapter promotes unless it beats the previous night.
The RegressionGate compares a candidate adapter's eval score against the
stored baseline (from GainTracker, P-73).  If the candidate does not
improve, it is blocked from promotion and the existing adapter stays in
place.

Promotion logic
---------------
- `baseline` — the most recent GainTracker score.  If none exists, the
  first night promotes unconditionally (nothing to beat).
- `candidate_score` — provided by the caller (from running `sera eval run`
  against the candidate adapter).  Range: 0.0–1.0.
- Promotion is allowed iff `candidate_score > baseline` (strict improvement).
- `PromotionResult` carries the decision, both scores, and a delta in pp.

The gate never mutates disk state — it only decides.  The caller is
responsible for moving adapter weights if `result.promoted` is True.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("sera.eval.regress")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionResult:
    promoted: bool
    candidate_score: float
    baseline_score: float | None
    delta_pp: float | None        # (candidate - baseline) * 100; None if no baseline
    reason: str

    @property
    def improvement_pp(self) -> float:
        """Positive = improvement; negative = regression."""
        if self.delta_pp is None:
            return 0.0
        return self.delta_pp


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class RegressionGate:
    """Promotes a LoRA adapter only when its eval score beats the baseline."""

    def __init__(
        self,
        gain_tracker=None,    # sera.train.lora.GainTracker instance or None
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._tracker = gain_tracker
        import time
        self._clock = clock or time.time

    def _baseline(self) -> float | None:
        """Return the most recent GainTracker accuracy, or None if no history."""
        if self._tracker is None:
            return None
        scores = self._tracker.scores()
        if not scores:
            return None
        return scores[-1][1]   # (date, accuracy) — take accuracy of latest entry

    def evaluate(
        self,
        candidate_score: float,
        adapter_dir: Path | None = None,
    ) -> PromotionResult:
        """Decide whether to promote the candidate adapter.

        Parameters
        ----------
        candidate_score:
            Eval accuracy of the candidate adapter (0.0–1.0).
        adapter_dir:
            Path to the candidate adapter weights (for logging only).
        """
        if not (0.0 <= candidate_score <= 1.0):
            raise ValueError(f"candidate_score must be in [0, 1], got {candidate_score}")

        baseline = self._baseline()

        if baseline is None:
            # No history — first night always promotes
            log.info("regress gate: no baseline, first-night promotion granted "
                     "(score=%.4f, adapter=%s)", candidate_score, adapter_dir)
            return PromotionResult(
                promoted=True,
                candidate_score=candidate_score,
                baseline_score=None,
                delta_pp=None,
                reason="no baseline — first night promotes unconditionally",
            )

        delta_pp = (candidate_score - baseline) * 100.0
        promoted = candidate_score > baseline

        if promoted:
            reason = (
                f"improvement: {delta_pp:+.2f}pp "
                f"({baseline:.4f} → {candidate_score:.4f})"
            )
            log.info("regress gate: PROMOTE %s (+%.2fpp)", adapter_dir, delta_pp)
        else:
            reason = (
                f"regression: {delta_pp:+.2f}pp "
                f"({baseline:.4f} → {candidate_score:.4f}); adapter blocked"
            )
            log.info("regress gate: BLOCK %s (%.2fpp)", adapter_dir, delta_pp)

        return PromotionResult(
            promoted=promoted,
            candidate_score=candidate_score,
            baseline_score=baseline,
            delta_pp=round(delta_pp, 4),
            reason=reason,
        )
