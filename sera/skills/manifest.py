"""Council dispatch for skills with council:true in their SKILL.md frontmatter.

Outclass over llm-council: their council is a standalone CLI tool; every call
costs the same model pool. Ours is surgical — only skills that declare
`council: true` run the ensemble, in-process, inside a single agent turn.
Skills without the flag pay zero overhead.

Usage:
  config = CouncilConfig(
      models=["model-a", "model-b", "model-c"],
      llm_factory=my_factory,
      council_skills=frozenset(["skill.research", "skill.analyze"]),
  )
  # Pass config to run_turn; the loop routes automatically.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from sera.council.chairman import run_chairman
from sera.council.confidence import compute_confidence
from sera.council.rank import RankingResult, parse_ranking
from sera.council.runner import CouncilRun, run_council

_RANKING_PROMPT = """\
Question: {question}

Evaluate these responses and rank them from best to worst.

{answer_block}

End your response with the ranking in this exact format:
FINAL RANKING:
1. [label]
2. [label]
{extra_lines}\
"""


@dataclass
class CouncilConfig:
    """Runtime parameters for council-aware run_turn.

    Attributes
    ----------
    models:
        Model IDs for council members. Length determines label count (A, B, C…).
    llm_factory:
        Called with a model_id; returns async callable `(prompt) -> str`.
        The cheap synthesis model should be one of these IDs.
    council_skills:
        Fully-qualified tool names (e.g. ``"skill.research"``) that should
        trigger the ensemble. Build this set with :func:`council_skills_from_disk`
        or by inspecting discovered skills directly.
    synthesis_model_id:
        Model used for chairman synthesis. Defaults to the first entry in
        ``models``; override with your cheapest available model.
    escalation_threshold:
        ``ConfidenceResult.should_escalate`` fires when mean Kendall-tau
        drops below this value. Default 0.3 (from P-34).
    """

    models: list[str]
    llm_factory: Callable[[str], Callable[[str], Awaitable[str]]]
    council_skills: frozenset[str] = field(default_factory=frozenset)
    synthesis_model_id: str = ""
    escalation_threshold: float = 0.3

    def __post_init__(self) -> None:
        if not self.synthesis_model_id and self.models:
            self.synthesis_model_id = self.models[0]


def council_skills_from_disk(root: Path) -> frozenset[str]:
    """Return the set of skill tool names that have ``council: true``.

    Convenience helper for CLI / eval harness initialisation.
    """
    from sera.skills.loader import SKILL_TOOL_PREFIX, discover_skills

    return frozenset(
        f"{SKILL_TOOL_PREFIX}{s.name}"
        for s in discover_skills(root)
        if s.council
    )


async def council_skill_dispatch(
    question: str,
    config: CouncilConfig,
) -> str:
    """Run the full 4-step council pipeline for one skill invocation.

    Steps
    -----
    1. Parallel answers via ``run_council``.
    2. Each model ranks the others' answers via ``parse_ranking``.
    3. Mean Kendall-tau confidence via ``compute_confidence``
       (``ConfidenceResult`` is returned for the caller to act on escalation;
       P-35 records it; escalation model-swap is deferred to P-36+).
    4. Borda-weighted chairman synthesis via ``run_chairman``.

    Returns the synthesis string. Falls back to the top-ranked raw answer
    if synthesis fails.
    """
    council_run = await run_council(question, config.models, config.llm_factory)

    valid_labels = tuple(a.label for a in council_run.answers)
    rankings = await _collect_rankings(council_run, config.llm_factory, valid_labels)

    # P-35: confidence computed (validates rankings); escalation model-swap deferred to P-36.
    compute_confidence(rankings, threshold=config.escalation_threshold)

    synthesis_llm = config.llm_factory(config.synthesis_model_id)
    chairman = await run_chairman(
        council_run, rankings, synthesis_llm, config.synthesis_model_id
    )
    return chairman.synthesis


async def _collect_rankings(
    council_run: CouncilRun,
    llm_factory: Callable[[str], Callable[[str], Awaitable[str]]],
    valid_labels: tuple[str, ...],
) -> list[RankingResult]:
    """Ask each council model to rank all answers anonymously.

    Only models with successful answers participate. Each ranker receives
    a prompt showing all labeled answers (their own included — they don't
    know which label is theirs).
    """
    answers_by_label = {
        a.label: a.content for a in council_run.answers if not a.error
    }
    if len(answers_by_label) < 2:
        return []

    answer_block = "\n\n".join(
        f"[{label}]: {content}"
        for label, content in sorted(answers_by_label.items())
    )
    n = len(valid_labels)
    extra = "\n".join(f"{i + 1}. [label]" for i in range(2, n))
    prompt = _RANKING_PROMPT.format(
        question=council_run.question,
        answer_block=answer_block,
        extra_lines=extra,
    )

    label_to_model = {v: k for k, v in council_run.label_map.items()}
    tasks = [
        _rank_one(label_to_model[label], prompt, llm_factory, valid_labels)
        for label in valid_labels
        if label in label_to_model
    ]
    return list(await asyncio.gather(*tasks))


async def _rank_one(
    model_id: str,
    prompt: str,
    llm_factory: Callable[[str], Callable[[str], Awaitable[str]]],
    valid_labels: tuple[str, ...],
) -> RankingResult:
    try:
        llm = llm_factory(model_id)
        text = await llm(prompt)
        return parse_ranking(text, valid_labels)
    except Exception:  # noqa: BLE001
        return RankingResult(
            ranking=(),
            raw_section="",
            is_complete=False,
            missing=frozenset(valid_labels),
            parse_method="none",
        )
