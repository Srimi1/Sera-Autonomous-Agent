"""Tests for sera.curator.meta — P-79 Curator-of-curators.

Phase verification: runaway curator throttled within one cycle.
"""
from __future__ import annotations

import pytest

from sera.curator.meta import MetaCurator


def _curator(max_p: int = 5, window_s: float = 60.0) -> tuple[MetaCurator, list[float]]:
    t = [0.0]
    mc = MetaCurator(max_proposals=max_p, window_s=window_s, clock=lambda: t[0])
    return mc, t


def _props(n: int) -> list[dict]:
    return [{"name": f"skill_{i}", "trigger": f"/s{i}"} for i in range(n)]


class TestMetaCurator:
    def test_accepts_within_limit(self) -> None:
        mc, t = _curator(max_p=5)
        result = mc.accept(_props(3))
        assert len(result.accepted) == 3
        assert len(result.dropped) == 0
        assert not result.was_throttled

    def test_drops_beyond_limit(self) -> None:
        mc, t = _curator(max_p=3)
        result = mc.accept(_props(5))
        assert len(result.accepted) == 3
        assert len(result.dropped) == 2
        assert result.was_throttled

    def test_throttle_reason_populated(self) -> None:
        mc, _ = _curator(max_p=2)
        result = mc.accept(_props(4))
        assert result.throttle_reason is not None
        assert result.throttle_reason.max_proposals == 2
        assert result.throttle_reason.message

    def test_is_throttled_after_limit(self) -> None:
        mc, _ = _curator(max_p=3)
        mc.accept(_props(3))
        assert mc.is_throttled()

    def test_not_throttled_below_limit(self) -> None:
        mc, _ = _curator(max_p=5)
        mc.accept(_props(2))
        assert not mc.is_throttled()

    def test_remaining_decrements(self) -> None:
        mc, _ = _curator(max_p=5)
        assert mc.remaining() == 5
        mc.accept(_props(3))
        assert mc.remaining() == 2

    def test_remaining_zero_when_throttled(self) -> None:
        mc, _ = _curator(max_p=3)
        mc.accept(_props(3))
        assert mc.remaining() == 0

    def test_window_expiry_resets_count(self) -> None:
        mc, t = _curator(max_p=3, window_s=60.0)
        mc.accept(_props(3))
        assert mc.is_throttled()
        t[0] = 61.0   # advance past window
        assert not mc.is_throttled()
        assert mc.remaining() == 3

    def test_partial_acceptance(self) -> None:
        mc, _ = _curator(max_p=2)
        mc.accept(_props(1))   # 1 accepted, 1 remaining
        result = mc.accept(_props(3))
        assert len(result.accepted) == 1
        assert len(result.dropped) == 2

    def test_reset_clears_state(self) -> None:
        mc, _ = _curator(max_p=2)
        mc.accept(_props(2))
        assert mc.is_throttled()
        mc.reset()
        assert not mc.is_throttled()
        assert mc.remaining() == 2

    def test_invalid_max_raises(self) -> None:
        with pytest.raises(ValueError):
            MetaCurator(max_proposals=0)

    def test_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            MetaCurator(window_s=-1.0)

    def test_accepted_count(self) -> None:
        mc, _ = _curator(max_p=10)
        mc.accept(_props(4))
        assert mc.accepted_count == 4


# ---------------------------------------------------------------------------
# THE VERIFICATION: runaway curator throttled within one cycle
# ---------------------------------------------------------------------------

class TestRunawayCuratorThrottled:
    def test_runaway_throttled_in_one_cycle(self) -> None:
        """Phase gate: 20 rapid proposals → only max_proposals accepted, rest dropped."""
        mc, _ = _curator(max_p=10, window_s=3600.0)

        # Simulate a runaway curator firing 20 proposals at once
        runaway_proposals = _props(20)
        result = mc.accept(runaway_proposals)

        assert len(result.accepted) == 10, (
            f"Expected 10 accepted, got {len(result.accepted)}"
        )
        assert len(result.dropped) == 10, (
            f"Expected 10 dropped, got {len(result.dropped)}"
        )
        assert result.was_throttled, "must report throttle when proposals are dropped"
        assert mc.is_throttled(), "curator must be throttled after runaway batch"

        # Further proposals in same window are fully blocked
        followup = mc.accept(_props(5))
        assert len(followup.accepted) == 0
        assert len(followup.dropped) == 5
