"""Skill A/B harness — cost × success-rate fitness.

Two skill variants → replay-verify each against a shared case list → pick the
winner by lex order (success_rate desc, total_cost asc). Winner gets
`lifecycle.verify(name)`; loser gets `lifecycle.archive(name)` so it stays
recoverable per P-24's revive contract.

Outclass: rivals pick a single variant by author intent and ship it. Sera
runs the ablation, surfaces the trade-off, and lets cost break ties. Loser
isn't deleted — `lifecycle.revive(loser)` flips it back to active any time
the user disagrees with the harness.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from sera.skills.lifecycle import SkillLifecycle
from sera.skills.loader import Skill
from sera.skills.verify import ReplayCase, replay_skill

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ABResult:
    """Aggregate replay outcome for one variant.

    `total_cost` is caller-supplied (tokens, ms wall, dollars — units are
    the caller's contract). Sera only uses it for tie-breaking.
    """

    name: str
    n_passed: int
    total_cases: int
    total_cost: float

    @property
    def success_rate(self) -> float:
        if self.total_cases <= 0:
            return 0.0
        return self.n_passed / float(self.total_cases)


@dataclass(frozen=True)
class Verdict:
    winner: str
    loser: str
    reason: str


def compute_verdict(a: ABResult, b: ABResult) -> Verdict:
    """Pure lex decision: higher success rate wins; tie → cheaper wins.

    Total tie → `a` wins (input order stable). Callers wanting random
    tie-break shuffle arg order before calling.
    """
    if a.success_rate > b.success_rate:
        return Verdict(
            winner=a.name, loser=b.name,
            reason=f"{a.name} higher success rate "
                   f"({a.success_rate:.2%} vs {b.success_rate:.2%})",
        )
    if b.success_rate > a.success_rate:
        return Verdict(
            winner=b.name, loser=a.name,
            reason=f"{b.name} higher success rate "
                   f"({b.success_rate:.2%} vs {a.success_rate:.2%})",
        )
    if a.total_cost < b.total_cost:
        return Verdict(
            winner=a.name, loser=b.name,
            reason=f"{a.name} cheaper at equal success "
                   f"(cost {a.total_cost:.3f} vs {b.total_cost:.3f})",
        )
    if b.total_cost < a.total_cost:
        return Verdict(
            winner=b.name, loser=a.name,
            reason=f"{b.name} cheaper at equal success "
                   f"(cost {b.total_cost:.3f} vs {a.total_cost:.3f})",
        )
    return Verdict(winner=a.name, loser=b.name, reason="exact tie — first arg wins")


# ─── Orchestrator ─────────────────────────────────────────────


@dataclass(frozen=True)
class Variant:
    """One side of an A/B match: a Skill plus a per-call cost estimate."""

    skill: Skill
    cost: float

    @property
    def name(self) -> str:
        return self.skill.name


async def run_ab(
    a: Variant,
    b: Variant,
    cases: Sequence[ReplayCase],
) -> tuple[ABResult, ABResult, Verdict]:
    """Replay every case against both variants; return aggregates + verdict.

    Each case runs once per variant — cost is multiplied across cases
    inside ABResult. Empty case list returns zero-pass results and the
    first-arg-wins tie verdict, so callers can detect "no signal" cleanly.
    """
    result_a = await _replay_variant(a, cases)
    result_b = await _replay_variant(b, cases)
    return result_a, result_b, compute_verdict(result_a, result_b)


async def _replay_variant(variant: Variant, cases: Sequence[ReplayCase]) -> ABResult:
    passed = 0
    for case in cases:
        outcome = await replay_skill(variant.skill, case)
        if outcome.passed:
            passed += 1
    return ABResult(
        name=variant.name,
        n_passed=passed,
        total_cases=len(cases),
        total_cost=variant.cost * len(cases),
    )


async def decide_and_persist(
    lifecycle: SkillLifecycle,
    a: Variant,
    b: Variant,
    cases: Sequence[ReplayCase],
    *,
    now: float | None = None,
) -> Verdict:
    """Run A/B, then verify winner + archive loser via the lifecycle store.

    Skips persistence when neither variant passes a single case — that's a
    "no signal" outcome; promoting either would be worse than leaving both
    in candidate. Returns the verdict for telemetry / CLI display either way.

    Loser is *archived*, never deleted. `lifecycle.revive(loser_name)` flips
    it back to ACTIVE — outclass: rivals delete the losing variant; Sera
    keeps the bytes + the audit trail.
    """
    result_a, result_b, verdict = await run_ab(a, b, cases)
    if result_a.n_passed == 0 and result_b.n_passed == 0:
        logger.info("ab: no signal — neither variant passed any case; lifecycle untouched")
        return verdict
    lifecycle.verify(verdict.winner, now=now)
    lifecycle.archive(verdict.loser, now=now)
    return verdict
