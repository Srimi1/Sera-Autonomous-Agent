"""Discovery agent — proactive skill proposal from session patterns.

Daily pass over recent sessions: if the same tool is called >= MIN_PATTERN_FREQUENCY
times in aggregate, the LLM is asked to propose a skill that codifies that pattern.

Outclass: Hermes curates what exists; Sera invents what's missing. The
discovery agent turns repeated manual behavior into named, triggered skills
without the user having to ask.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MIN_PATTERN_FREQUENCY = 3
"""Minimum tool-call occurrences across sessions to trigger LLM analysis."""

_DISCOVERY_PROMPT_TEMPLATE = (
    "You are Sera's discovery agent. Below is a summary of tool-call "
    "patterns observed across recent sessions. For each pattern that "
    "suggests a repeatable user workflow, propose a new skill.\n\n"
    'Output JSON of the form:\n'
    '{"proposals": [{"trigger": "/<slug>", "name": "<snake_case>", '
    '"description": "<one sentence>", "body_hint": "<short markdown body>", '
    '"reasoning": "<why this pattern warrants a skill>"}]}\n\n'
    "Only propose skills NOT in the known-triggers list. "
    "Empty proposals list is fine if nothing qualifies.\n\n"
    "Known triggers already covered:\n%s\n\n"
    "Observed patterns (tool → count):\n%s\n"
)


@dataclass(frozen=True)
class DiscoveryProposal:
    trigger: str
    name: str
    description: str
    body_hint: str
    source_session_ids: list[str] = field(default_factory=list)
    reasoning: str = ""
    frequency: int = 0


@dataclass(frozen=True)
class DiscoveryRun:
    proposals: tuple[DiscoveryProposal, ...]
    sessions_scanned: int
    known_triggers: frozenset[str]
    ran_at: float
    error: str | None = None


# ─── Heuristic: tool call frequency ───────────────────────────────


def tool_pattern_counts(sessions: list[Any]) -> dict[str, int]:
    """Count per-tool call occurrences across all sessions.

    Works with real `Session` objects or any object whose `.messages`
    list contains items with `.tool_calls` (list of dicts). Ignores
    messages that aren't assistant-role.
    """
    counts: dict[str, int] = {}
    for session in sessions:
        for msg in getattr(session, "messages", []):
            if getattr(msg, "role", None) != "assistant":
                continue
            for tc in getattr(msg, "tool_calls", None) or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name")
                if name:
                    counts[name] = counts.get(name, 0) + 1
    return counts


# ─── Discovery agent ───────────────────────────────────────────────


class DiscoveryAgent:
    """LLM-powered discovery. Skips LLM when no patterns exceed threshold."""

    def __init__(self, llm_call: Callable[[str], Awaitable[Any]]) -> None:
        self._llm = llm_call

    async def run(
        self,
        sessions: list[Any],
        *,
        known_triggers: set[str] | frozenset[str],
    ) -> DiscoveryRun:
        started = time.time()
        known = frozenset(known_triggers)

        counts = tool_pattern_counts(sessions)
        hot = {tool: n for tool, n in counts.items() if n >= MIN_PATTERN_FREQUENCY}

        if not hot:
            return DiscoveryRun(
                proposals=(),
                sessions_scanned=len(sessions),
                known_triggers=known,
                ran_at=started,
            )

        pattern_lines = "\n".join(f"  {tool}: {n}" for tool, n in sorted(hot.items()))
        known_lines = ", ".join(sorted(known)) if known else "(none)"
        prompt = _DISCOVERY_PROMPT_TEMPLATE % (known_lines, pattern_lines)

        try:
            raw = await self._llm(prompt)
            proposals = _parse_proposals(raw, known_triggers=known)
            return DiscoveryRun(
                proposals=proposals,
                sessions_scanned=len(sessions),
                known_triggers=known,
                ran_at=started,
            )
        except Exception as e:  # noqa: BLE001 — discovery is best-effort
            logger.info("discovery run failed: %s", e)
            return DiscoveryRun(
                proposals=(),
                sessions_scanned=len(sessions),
                known_triggers=known,
                ran_at=started,
                error=str(e),
            )


def _parse_proposals(
    raw: Any,
    *,
    known_triggers: frozenset[str],
) -> tuple[DiscoveryProposal, ...]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"discovery output is not valid JSON: {e}") from e
    elif isinstance(raw, dict):
        data = raw
    else:
        raise TypeError(f"discovery output must be str | dict, got {type(raw).__name__}")

    out: list[DiscoveryProposal] = []
    for p in data.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        trigger = str(p.get("trigger") or "")
        if not trigger:
            continue
        if trigger in known_triggers:
            continue
        out.append(
            DiscoveryProposal(
                trigger=trigger,
                name=str(p.get("name") or ""),
                description=str(p.get("description") or ""),
                body_hint=str(p.get("body_hint") or ""),
                reasoning=str(p.get("reasoning") or ""),
            )
        )
    return tuple(out)


# ─── Convenience entry point ──────────────────────────────────────


async def run_discovery(
    sessions: list[Any],
    *,
    known_triggers: set[str] | frozenset[str] | None = None,
    llm_call: Callable[[str], Awaitable[Any]] | None = None,
) -> DiscoveryRun:
    """One-liner daily-pass entry point.

    Requires `llm_call` to produce non-empty proposals; without it the
    heuristic frequency check still runs and returns an empty run.
    """
    if llm_call is None:
        async def _noop(prompt: str) -> str:
            return '{"proposals": []}'
        llm_call = _noop

    agent = DiscoveryAgent(llm_call=llm_call)
    return await agent.run(sessions, known_triggers=known_triggers or set())
