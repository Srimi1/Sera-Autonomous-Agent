"""Council runner — N models answer in parallel, anonymous A/B/C labels.

Outclass: llm-council is a standalone CLI tool. Sera's council fires inside
a single agent turn. The caller sees one synthesized answer; the ensemble
ran invisibly. Per-skill opt-in means zero overhead on every call that
doesn't need it.

Architecture:
  - Labels are randomly assigned each run — no model is always "A".
  - Each model receives only its own label; never the others' IDs.
  - One model failure does not crash the run; partial results surface.
  - `label_map` (model → label) is sealed in CouncilRun for the chairman
    to use during synthesis after ranking.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

COUNCIL_LABELS: tuple[str, ...] = ("A", "B", "C", "D", "E")
"""Pool of anonymous labels. Runs use the first N."""

_MEMBER_PROMPT = (
    "You are council member {label}. Answer the question below concisely "
    "and completely. Do not identify yourself by any model name or provider.\n\n"
    "Question: {question}"
)


@dataclass(frozen=True)
class CouncilAnswer:
    label: str          # "A", "B", or "C" — never the model ID
    content: str        # answer text; empty string on failure
    latency_ms: float   # wall-clock ms for this member's call
    error: str | None = None


@dataclass(frozen=True)
class CouncilRun:
    question: str
    answers: tuple[CouncilAnswer, ...]
    label_map: dict[str, str]  # model_id → label (sealed until synthesis)
    ran_at: float


async def run_council(
    question: str,
    models: list[str],
    llm_factory: Callable[[str], Callable[[str], Awaitable[str]]],
) -> CouncilRun:
    """Run `models` in parallel with randomised anonymous labels.

    `llm_factory(model_id)` returns an async callable `(prompt) -> str`.
    Each model's prompt contains only its own label — never others' IDs.
    One model failure produces a CouncilAnswer with error set; others proceed.
    """
    labels = list(COUNCIL_LABELS[: len(models)])
    shuffled_labels = labels[:]
    random.shuffle(shuffled_labels)
    label_map: dict[str, str] = {
        model: label for model, label in zip(models, shuffled_labels)
    }
    ran_at = time.time()
    answers = await asyncio.gather(
        *[_call_member(question, model, label, llm_factory) for model, label in label_map.items()]
    )
    return CouncilRun(
        question=question,
        answers=tuple(answers),
        label_map=label_map,
        ran_at=ran_at,
    )


async def _call_member(
    question: str,
    model_id: str,
    label: str,
    llm_factory: Callable[[str], Callable[[str], Awaitable[str]]],
) -> CouncilAnswer:
    prompt = _MEMBER_PROMPT.format(label=label, question=question)
    t0 = time.monotonic()
    try:
        llm = llm_factory(model_id)
        content = await llm(prompt)
        return CouncilAnswer(
            label=label,
            content=content,
            latency_ms=(time.monotonic() - t0) * 1000,
        )
    except Exception as e:  # noqa: BLE001 — one member must not kill the council
        return CouncilAnswer(
            label=label,
            content="",
            latency_ms=(time.monotonic() - t0) * 1000,
            error=str(e),
        )
