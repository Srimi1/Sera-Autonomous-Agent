"""Thompson-sampling bandit router.

Picks the best LLM profile per task_kind using Beta(alpha, beta) distributions.
Cheap profiles converge to easy task kinds; big profiles converge to hard ones.

Heritage: no rival ships per-task-kind bandit routing — this is the outclass.
"""
from __future__ import annotations

import random
from typing import Any


def reward_signal(
    *,
    success: bool,
    latency_ms: int,
    cost_usd: float,
    latency_budget_ms: int = 10_000,
    cost_budget_usd: float = 0.005,
) -> float:
    """Map observed call outcome to a binary reward in {0.0, 1.0}.

    A call earns reward=1 when it succeeds, stays within latency budget,
    and stays within cost budget. All three gates must pass.
    """
    if not success:
        return 0.0
    if latency_ms > latency_budget_ms:
        return 0.0
    if cost_usd > cost_budget_usd:
        return 0.0
    return 1.0


class ThompsonBandit:
    """Beta-distribution bandit over LLM profiles per task_kind.

    Each arm is a (profile, task_kind) pair.
    Prior: Beta(1, 1) = Uniform (Jeffreys uninformative prior).
    Update: alpha += reward, beta += (1 - reward).
    Pick: sample each arm, return argmax.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        # (profile, task_kind) → [alpha, beta]
        self._arms: dict[tuple[str, str], list[float]] = {}
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    # Core bandit interface
    # ------------------------------------------------------------------

    def pick(self, task_kind: str, profiles: list[str]) -> str:
        """Return the profile with the highest Thompson sample for task_kind.

        Ties broken by first occurrence; in practice rare after enough updates.
        """
        if not profiles:
            raise ValueError("profiles must be non-empty")
        if len(profiles) == 1:
            return profiles[0]
        best_profile = profiles[0]
        best_sample = -1.0
        for p in profiles:
            a, b = self._arm(p, task_kind)
            sample = self._rng.betavariate(a, b)
            if sample > best_sample:
                best_sample = sample
                best_profile = p
        return best_profile

    def update(self, profile: str, task_kind: str, *, reward: float) -> None:
        """Update arm after observing a reward.

        reward must be in [0.0, 1.0]. Use binary 0.0 or 1.0 for Thompson;
        fractional values are also accepted (e.g. partial credit).
        """
        if not 0.0 <= reward <= 1.0:
            raise ValueError(f"reward must be in [0, 1], got {reward!r}")
        arm = self._arm(profile, task_kind)
        arm[0] += reward
        arm[1] += 1.0 - reward

    def mean(self, profile: str, task_kind: str) -> float:
        """Expected reward = alpha / (alpha + beta)."""
        a, b = self._arm(profile, task_kind)
        return a / (a + b)

    # ------------------------------------------------------------------
    # Cold-start seeding from P-36 router_stats
    # ------------------------------------------------------------------

    def seed_from_stats(
        self,
        profile_models: dict[str, str],
        *,
        _db=None,
    ) -> int:
        """Initialise Beta priors from historical router_stats data.

        profile_models: maps profile_name → model string
                        e.g. {"cheap": "gpt-4o-mini", "big": "claude-sonnet-4-6"}

        Returns the number of stat rows consumed.
        """
        from sera.llm.router_stats import p50_table

        model_to_profile = {model: profile for profile, model in profile_models.items()}
        rows = p50_table(_db)
        loaded = 0
        for r in rows:
            profile = model_to_profile.get(r["model"])
            if profile is None:
                continue
            n = r["n"]
            n_ok = round(n * r["success_pct"] / 100)
            n_fail = n - n_ok
            arm = self._arm(profile, r["task_kind"])
            arm[0] += n_ok
            arm[1] += n_fail
            loaded += 1
        return loaded

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Snapshot of all arm distributions for debugging."""
        return {
            f"{profile}/{task_kind}": {
                "alpha": a,
                "beta": b,
                "mean": a / (a + b),
                "n": int(a + b - 2),  # subtract prior
            }
            for (profile, task_kind), [a, b] in sorted(self._arms.items())
        }

    def best_profile(self, task_kind: str, profiles: list[str]) -> str:
        """Return the profile with the highest mean reward (greedy, no sampling).

        Use for reporting; use pick() for actual routing.
        """
        return max(profiles, key=lambda p: self.mean(p, task_kind))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _arm(self, profile: str, task_kind: str) -> list[float]:
        key = (profile, task_kind)
        if key not in self._arms:
            self._arms[key] = [1.0, 1.0]
        return self._arms[key]
