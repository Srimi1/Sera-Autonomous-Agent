"""Cross-session consolidation — P-77.

OUTCLASS: Contradictions surface to the user.  No rival scans its own
memory for internal conflicts and presents them as a single reconciliation
prompt.  Sera does.

The engine takes a list of fact statements (extracted from dream summaries
or entity relations) and asks an LLM to find contradictions.  When ≥1
contradiction is found it produces a single `reconciliation_prompt` string
the user can answer.  Multiple contradictions are collapsed into one prompt
so the user is never spammed.

LLM contract (same injectable seam as the dream journal)
---------------------------------------------------------
    await llm_call(prompt: str) -> str | dict
    Response JSON: {"contradictions": [{"a": "...", "b": "...", "reason": "..."}]}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("sera.dream.consolidate")

LLMCall = Callable[[str], Awaitable[object]]

_CONTRADICTION_PROMPT = (
    "You are Sera's cross-session consolidation engine.  Below is a list of "
    "facts extracted from the user's sessions.  Identify any pairs of facts "
    "that directly contradict each other.  Do NOT list minor rephrasing — only "
    "genuine logical conflicts (e.g. two different values for the same attribute).\n\n"
    "Reply as JSON:\n"
    '{{"contradictions": [{{"a": "fact one", "b": "fact two", "reason": "why they conflict"}}]}}\n\n'
    "FACTS:\n{facts}\n"
)

_RECONCILE_INTRO = (
    "Sera found {n} contradiction{s} in your recent sessions and needs your help:\n\n"
)
_RECONCILE_ITEM = "{i}. {a!r}  ↔  {b!r}\n   ({reason})\n"


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Contradiction:
    a: str
    b: str
    reason: str


@dataclass
class ConsolidationResult:
    contradictions: list[Contradiction] = field(default_factory=list)
    reconciliation_prompt: str = ""
    error: str | None = None

    @property
    def has_conflicts(self) -> bool:
        return bool(self.contradictions)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _coerce_json(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return json.loads(str(raw))


def _build_reconciliation_prompt(contradictions: list[Contradiction]) -> str:
    n = len(contradictions)
    s = "s" if n != 1 else ""
    parts = [_RECONCILE_INTRO.format(n=n, s=s)]
    for i, c in enumerate(contradictions, 1):
        parts.append(_RECONCILE_ITEM.format(i=i, a=c.a, b=c.b, reason=c.reason))
    parts.append("\nWhich is correct, or should both be kept?")
    return "".join(parts)


class ConsolidationEngine:
    """Scans fact statements for contradictions; returns a single user prompt."""

    def __init__(self, llm_call: LLMCall) -> None:
        self._llm = llm_call

    async def consolidate(self, facts: list[str]) -> ConsolidationResult:
        """Find contradictions in `facts`; return ConsolidationResult."""
        if len(facts) < 2:
            return ConsolidationResult()

        fact_block = "\n".join(f"- {f}" for f in facts)
        prompt = _CONTRADICTION_PROMPT.format(facts=fact_block)

        try:
            raw = await self._llm(prompt)
            data = _coerce_json(raw)
        except Exception as exc:
            log.info("consolidation LLM failed: %s", exc)
            return ConsolidationResult(error=str(exc))

        contradictions: list[Contradiction] = []
        for item in data.get("contradictions") or []:
            if not isinstance(item, dict):
                continue
            a = str(item.get("a") or "").strip()
            b = str(item.get("b") or "").strip()
            r = str(item.get("reason") or "").strip()
            if a and b:
                contradictions.append(Contradiction(a=a, b=b, reason=r))

        prompt_out = _build_reconciliation_prompt(contradictions) if contradictions else ""
        return ConsolidationResult(
            contradictions=contradictions,
            reconciliation_prompt=prompt_out,
        )
