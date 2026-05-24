"""Curator-of-curators — throttles runaway skill churn (P-79).

OUTCLASS: Discovery agents can propose skills faster than any human can
review them.  The meta-curator imposes a hard rate limit per window: if
more than `max_proposals` skills are proposed within `window_s` seconds,
the curator is throttled and further proposals are dropped until the
window expires.  Nobody else ships this — they let churn accumulate.

The MetaCurator wraps any proposal source (DiscoveryAgent, BackgroundCurator,
or any callable that returns a list of proposals) and gates its output.

Rate-limit design
-----------------
- Sliding-window counter: timestamps of accepted proposals in the last
  `window_s` seconds.
- When the count reaches `max_proposals`, the source is throttled for the
  remainder of the window.
- `is_throttled()` — check without consuming.
- `accept(proposals)` — filter proposals, record accepted timestamps,
  return accepted list.
- `ThrottleReason` attached to dropped proposals for audit.
- Clock is injectable for tests.
"""
from __future__ import annotations

import time as _time_mod
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThrottleReason:
    window_s: float
    max_proposals: int
    current_count: int
    message: str = ""


@dataclass
class MetaCuratorResult:
    accepted: list[Any] = field(default_factory=list)
    dropped: list[Any] = field(default_factory=list)
    throttle_reason: ThrottleReason | None = None

    @property
    def was_throttled(self) -> bool:
        return self.throttle_reason is not None


# ---------------------------------------------------------------------------
# Meta-curator
# ---------------------------------------------------------------------------

class MetaCurator:
    """Rate-limits skill proposals; throttles runaway curators within one cycle."""

    def __init__(
        self,
        max_proposals: int = 10,
        window_s: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_proposals < 1:
            raise ValueError("max_proposals must be ≥ 1")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self._max = max_proposals
        self._window = window_s
        self._clock = clock or _time_mod.time
        self._timestamps: list[float] = []

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Drop timestamps outside the current window."""
        cutoff = self._clock() - self._window
        self._timestamps = [t for t in self._timestamps if t >= cutoff]

    def _in_window(self) -> int:
        self._prune()
        return len(self._timestamps)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_throttled(self) -> bool:
        """Return True if the rate limit is currently exceeded."""
        return self._in_window() >= self._max

    def remaining(self) -> int:
        """How many proposals can still be accepted this window."""
        return max(0, self._max - self._in_window())

    def accept(self, proposals: list[Any]) -> MetaCuratorResult:
        """Filter proposals against the rate limit.

        Accepted proposals are timestamped.  Dropped proposals are returned
        with a ThrottleReason.  Partial acceptance is possible: if 3 slots
        remain and 5 proposals arrive, 3 are accepted and 2 are dropped.
        """
        result = MetaCuratorResult()

        for p in proposals:
            if self._in_window() < self._max:
                self._timestamps.append(self._clock())
                result.accepted.append(p)
            else:
                result.dropped.append(p)

        if result.dropped:
            result.throttle_reason = ThrottleReason(
                window_s=self._window,
                max_proposals=self._max,
                current_count=self._in_window(),
                message=(
                    f"Rate limit reached: {self._max} proposals per "
                    f"{self._window:.0f}s window. "
                    f"{len(result.dropped)} proposal(s) dropped."
                ),
            )

        return result

    def reset(self) -> None:
        """Clear all timestamps (for testing or manual override)."""
        self._timestamps = []

    @property
    def accepted_count(self) -> int:
        return self._in_window()
