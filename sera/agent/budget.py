"""Shared iteration budget with one-shot grace call.

Outclass: Hermes tracks iterations per-agent. When the parent agent caps,
its in-flight subagent reply is still discarded mid-stream — wasted spend
and a truncated user-facing answer. Sera shares one budget across the
parent and (future) subagents, and reserves a single **grace** iteration
at the exhaustion boundary so the model can produce a clean summary
instead of stopping mid-thought.

Lifecycle of a turn:

  remaining > 0           → normal iteration, `consume()` decrements
  remaining == 0          → caller invokes `request_grace()` once;
                            sets `grace_used = True`, refunds 1 iteration
  remaining == 0 + used   → `consume()` raises MaxIterations
"""
from __future__ import annotations

from dataclasses import dataclass


class MaxIterations(RuntimeError):
    """Budget exhausted, grace already spent."""


@dataclass
class IterationBudget:
    """Mutable counter shared by every agent in a single user turn.

    `total` is the hard cap (parent + all subagents combined).
    `remaining` decreases with `consume()`.
    `grace_used` flips True the first (and only) time `request_grace()` is
    called at remaining == 0 — that call refunds 1 iteration for a final
    summarize-and-exit pass.
    """

    total: int
    remaining: int
    grace_used: bool = False

    @classmethod
    def of(cls, total: int) -> "IterationBudget":
        if total < 1:
            raise ValueError(f"budget total must be >= 1, got {total}")
        return cls(total=total, remaining=total)

    def consume(self) -> None:
        """Spend one iteration. Raises MaxIterations once exhausted past grace."""
        if self.remaining <= 0:
            raise MaxIterations(
                f"iteration budget exhausted (total={self.total}, "
                f"grace_used={self.grace_used})"
            )
        self.remaining -= 1

    def can_request_grace(self) -> bool:
        """True iff next iteration would exhaust and grace is still available."""
        return self.remaining == 0 and not self.grace_used

    def request_grace(self) -> None:
        """Grant the one-shot summarize-and-exit iteration.

        Idempotent in the sense that a second call after `grace_used` is True
        is a programming error and raises — the loop is supposed to check
        `can_request_grace()` first.
        """
        if self.grace_used:
            raise RuntimeError("grace already used for this budget")
        self.grace_used = True
        self.remaining = 1
