"""Council confidence metric — mean Kendall-tau + cost-aware escalation.

Outclass over llm-council: they have no confidence metric at all. Every run
costs the same regardless of whether the council agreed or disagreed. Ours:
  - Computes mean pairwise Kendall-tau across all complete rankings.
  - tau = 1.0 → perfect agreement. tau = -1.0 → perfect reversal.
  - should_escalate = (tau < threshold). Default threshold = 0.3.
  - Only escalates on genuine disagreement — not on missing data.
  - Typed ConfidenceResult with pairs_evaluated for transparency.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from sera.council.rank import RankingResult

_DEFAULT_THRESHOLD = 0.3


@dataclass(frozen=True)
class ConfidenceResult:
    tau: float             # mean pairwise Kendall-tau; range [-1.0, 1.0]
    should_escalate: bool  # True when tau < threshold
    threshold: float       # threshold used for this result
    pairs_evaluated: int   # number of (ri, rj) pairs that contributed
    complete_rankings: int # count of rankings with is_complete=True


def compute_confidence(
    rankings: list[RankingResult],
    threshold: float = _DEFAULT_THRESHOLD,
) -> ConfidenceResult:
    """Compute mean Kendall-tau across complete ranking pairs.

    Parameters
    ----------
    rankings:
        RankingResults from rank.parse_ranking. Incomplete results are skipped.
    threshold:
        Escalation fires when mean tau falls below this value. Default 0.3.

    Returns
    -------
    ConfidenceResult
        tau=1.0, no escalation when only 0–1 complete rankings exist
        (no disagreement is possible; escalation is inappropriate).
    """
    complete = [r for r in rankings if r.is_complete]
    n = len(complete)

    if n < 2:
        tau = 1.0  # no pair exists to disagree; assume full confidence
        return ConfidenceResult(
            tau=tau,
            should_escalate=False,
            threshold=threshold,
            pairs_evaluated=0,
            complete_rankings=n,
        )

    pair_taus = [
        _kendall_tau(r1.ranking, r2.ranking)
        for r1, r2 in combinations(complete, 2)
    ]
    mean_tau = sum(pair_taus) / len(pair_taus)

    return ConfidenceResult(
        tau=mean_tau,
        should_escalate=mean_tau < threshold,
        threshold=threshold,
        pairs_evaluated=len(pair_taus),
        complete_rankings=n,
    )


def _kendall_tau(r1: tuple[str, ...], r2: tuple[str, ...]) -> float:
    """Kendall-tau-b between two complete rankings of the same label set.

    For each pair of items (a, b), a pair is concordant when both rankings
    agree on relative order and discordant when they disagree. Tied positions
    (same label at same slot in both rankings) are impossible here since
    rankings are permutations; all ties are cross-ranking ties handled by
    tau-b's denominator, which reduces to tau-a for complete permutations.
    """
    pos1 = {label: i for i, label in enumerate(r1)}
    pos2 = {label: i for i, label in enumerate(r2)}
    labels = list(pos1.keys())
    n = len(labels)

    if n < 2:
        return 1.0

    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            a, b = labels[i], labels[j]
            d1 = pos1[a] - pos1[b]
            d2 = pos2[a] - pos2[b]
            product = d1 * d2
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1

    n_pairs = n * (n - 1) // 2
    return (concordant - discordant) / n_pairs
