"""Chairman synthesis — Borda aggregation + cheap-model final answer.

Outclass over llm-council:
  - Anonymity preserved end-to-end: chairman sees sorted content, never model IDs
    or labels. llm-council passes "Model: gpt-4" directly into the synthesis prompt.
  - Borda count aggregates rankings before synthesis — chairman gets a structured
    ranked list, not raw ranking text it must re-parse.
  - Synthesizer is the cheap model: pluggable via synthesis_llm + synthesis_model_id.
    llm-council hardcodes CHAIRMAN_MODEL with no cost-aware swap.
  - Typed ChairmanResult with borda_scores for diagnostics and rankings_used for
    partial-failure accounting. llm-council returns a plain dict.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from sera.council.rank import RankingResult
from sera.council.runner import CouncilRun

_SYNTHESIS_PROMPT = """\
Council evaluated: "{question}"

Answers ranked by peer consensus (best first):

{ranked_answers}

Synthesize a final answer that incorporates the best insights, \
prioritising the top-ranked content.\
"""


@dataclass(frozen=True)
class ChairmanResult:
    winner_label: str          # anonymous label with highest Borda score
    winner_model: str | None   # resolved model_id; None if label_map absent
    synthesis: str             # final answer from cheap synthesis LLM
    borda_scores: dict[str, int]  # label → Borda score (all labels)
    rankings_used: int         # number of complete RankingResults consumed
    synthesis_model: str       # model_id used for synthesis (cost auditing)


async def run_chairman(
    council_run: CouncilRun,
    rankings: list[RankingResult],
    synthesis_llm: Callable[[str], Awaitable[str]],
    synthesis_model_id: str = "cheap",
) -> ChairmanResult:
    """Aggregate peer rankings and call the cheap model for final synthesis.

    Parameters
    ----------
    council_run:
        Completed CouncilRun from runner.py — provides the question, answers
        by label, and the label_map for winner resolution.
    rankings:
        List of RankingResults from rank.parse_ranking. Incomplete rankings
        (is_complete=False) are silently skipped.
    synthesis_llm:
        Async callable `(prompt) -> str`. Should be the cheapest available
        model; it synthesises from pre-ranked content, not raw model outputs.
    synthesis_model_id:
        Human-readable ID stored in ChairmanResult for cost auditing.

    Returns
    -------
    ChairmanResult
        Always returns; never raises. If all rankings failed, returns the
        first successful answer as winner with empty synthesis on LLM error.
    """
    labels = [a.label for a in council_run.answers]
    borda_scores = _borda_count(rankings, labels)
    winner_label = _pick_winner(borda_scores)

    label_to_model = {v: k for k, v in council_run.label_map.items()}
    winner_model = label_to_model.get(winner_label)

    answer_by_label = {a.label: a.content for a in council_run.answers}
    sorted_labels = sorted(borda_scores, key=lambda l: (-borda_scores[l], l))
    ranked_answers = "\n\n".join(
        f"[{rank + 1}] {answer_by_label.get(label, '(no answer)')}"
        for rank, label in enumerate(sorted_labels)
        if answer_by_label.get(label)
    )

    prompt = _SYNTHESIS_PROMPT.format(
        question=council_run.question,
        ranked_answers=ranked_answers,
    )
    try:
        synthesis = await synthesis_llm(prompt)
    except Exception:  # noqa: BLE001 — synthesis failure must not crash
        synthesis = answer_by_label.get(winner_label, "")

    return ChairmanResult(
        winner_label=winner_label,
        winner_model=winner_model,
        synthesis=synthesis,
        borda_scores=borda_scores,
        rankings_used=sum(1 for r in rankings if r.is_complete),
        synthesis_model=synthesis_model_id,
    )


def _borda_count(
    rankings: list[RankingResult],
    labels: list[str],
) -> dict[str, int]:
    """Compute Borda scores from complete rankings.

    For N candidates, position i (0-indexed) earns (N-1-i) points.
    Incomplete rankings are skipped; each complete ranking must contain
    exactly the same label set.
    """
    scores: dict[str, int] = {label: 0 for label in labels}
    n = len(labels)
    for result in rankings:
        if not result.is_complete:
            continue
        for i, label in enumerate(result.ranking):
            if label in scores:
                scores[label] += n - 1 - i
    return scores


def _pick_winner(borda_scores: dict[str, int]) -> str:
    """Return label with highest Borda score; alphabetical tie-break."""
    if not borda_scores:
        raise ValueError("borda_scores is empty")
    return min(
        borda_scores,
        key=lambda label: (-borda_scores[label], label),
    )
