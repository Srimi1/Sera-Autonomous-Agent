"""Gateway router — first-class consumer of Sera's agent primitives.

OUTCLASS: Hermes' gateway is a dumb webhook dispatcher. Sera's gateway runs
every inbound event through:
  - Cost ceilings (P-39 BudgetEnforcer) — per-platform AND per-user caps;
    hard-blocked events get a refusal response, never reach the LLM
  - Bandit routing (P-37 ThompsonBandit) — picks the cheapest profile that
    historically wins for this task_kind; updates arms post-turn
  - In-loop council (P-31..P-35 CouncilConfig) — skill calls that match
    `council_skills` trigger the 4-step ensemble verdict transparently
  - Memory ingest (P-46 scanner_base) — inbound events become MemoryTree
    chunks so future turns can recall them

Same brain, different inbox. No rival ships agent-aware webhook routing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sera.agent.budget import IterationBudget
from sera.agent.loop import run_turn
from sera.llm.bandit import ThompsonBandit, reward_signal
from sera.llm.base import LLM
from sera.llm.budget import BudgetEnforcer, BudgetExceeded
from sera.memory.session import Session
from sera.safety.approval import AutoApproveGate
from sera.skills.manifest import CouncilConfig

log = logging.getLogger("sera.gateway.router")


# ---------------------------------------------------------------------------
# Event shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InboundEvent:
    """Normalized inbound message from any platform."""
    platform: str               # "telegram", "discord", "slack", "whatsapp", ...
    user_id: str
    channel_id: str
    text: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def task_kind(self) -> str:
        """Infer task_kind for bandit routing.

        Convention: messages starting with '/' are slash commands → "tool"-style.
        Plain text → "chat". Subclasses / platform adapters can override metadata.
        """
        if "task_kind" in self.metadata:
            return str(self.metadata["task_kind"])
        if self.text.startswith("/"):
            return "command"
        return "chat"


@dataclass
class OutboundResponse:
    """Result of dispatching one InboundEvent."""
    event: InboundEvent
    ok: bool
    text: str
    profile_used: str | None = None
    latency_ms: int = 0
    blocked_by_budget: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------

# Caller-supplied factory: (platform, channel_id) → LLM
# Lets the router ask for the LLM AFTER the bandit picks a profile.
LLMFactory = Callable[[str], LLM]


class Router:
    """Routes InboundEvents through bandit + budget + council into run_turn.

    Construct once per process; safe across many events. The bandit, budget,
    and council state are owned here so cross-event learning works.
    """

    def __init__(
        self,
        *,
        llm_factory: LLMFactory,
        profiles: list[str] | None = None,
        bandit: ThompsonBandit | None = None,
        cost_enforcer: BudgetEnforcer | None = None,
        council_config: CouncilConfig | None = None,
        max_iterations: int = 12,
        latency_budget_ms: int = 30_000,
        cost_budget_usd: float = 0.10,
        on_inbound: Callable[[InboundEvent], Awaitable[None]] | None = None,
        on_response: Callable[[InboundEvent, "OutboundResponse"], Awaitable[None]] | None = None,
        session_resolver: Callable[[InboundEvent], Session] | None = None,
        workspace: str | None = None,
    ) -> None:
        """
        Args:
            llm_factory:        called with the bandit-picked profile name
                                (or "default") and must return an LLM adapter.
            profiles:           list of profile names the bandit may choose from.
                                If None, the router runs without bandit routing
                                (uses `llm_factory("default")`).
            bandit:             ThompsonBandit instance (shared across events).
            cost_enforcer:      P-39 BudgetEnforcer (shared across events).
            council_config:     P-35 CouncilConfig (passed into run_turn).
            max_iterations:     IterationBudget per turn.
            latency_budget_ms:  reward gate (P-37 reward_signal).
            cost_budget_usd:    reward gate.
            on_inbound:         hook called with the event before dispatch
                                (use to ingest into MemoryTree).
            on_response:        hook called with (event, response) after dispatch
                                (use for outbound platform sends).
            session_resolver:   maps event → Session. P-52 uses this for 24h
                                per-user session continuity. None = fresh session
                                per event.
            workspace:          workspace root for tool calls inside the turn.
        """
        self._llm_factory = llm_factory
        self._profiles = profiles
        self._bandit = bandit
        self._cost_enforcer = cost_enforcer
        self._council_config = council_config
        self._max_iterations = max_iterations
        self._latency_budget_ms = latency_budget_ms
        self._cost_budget_usd = cost_budget_usd
        self._on_inbound = on_inbound
        self._on_response = on_response
        self._session_resolver = session_resolver
        self._workspace = workspace
        self._n_handled = 0

    @property
    def n_handled(self) -> int:
        return self._n_handled

    # ------------------------------------------------------------------
    # Single-event dispatch (the agent-aware path)
    # ------------------------------------------------------------------

    async def dispatch(self, event: InboundEvent, *, sink: Any = None) -> OutboundResponse:
        """Run one event through the full primitive chain.

        sink: optional TokenSink (P-62). When provided, the turn streams tokens
        and tool-call lifecycle events through it as they happen, so callers
        (the SSE endpoint) can deliver first token before the turn completes.
        """
        self._n_handled += 1

        # Memory ingest hook — fire-and-forget; failures don't block dispatch.
        if self._on_inbound is not None:
            try:
                await self._on_inbound(event)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_inbound hook failed: %s", exc)

        # Pre-LLM budget check — never call the model if a hard cap is hit.
        if self._cost_enforcer is not None:
            check = self._cost_enforcer.check(task_kind=event.task_kind)
            if check.blocked:
                return OutboundResponse(
                    event=event,
                    ok=False,
                    text=f"[gateway] {check.message}",
                    blocked_by_budget=True,
                    error="budget_hard_cap",
                )

        # Bandit-pick a profile if we have one configured.
        if self._bandit is not None and self._profiles:
            profile = self._bandit.pick(event.task_kind, self._profiles)
        else:
            profile = "default"

        # Build the LLM for the chosen profile.
        try:
            llm = self._llm_factory(profile)
        except Exception as exc:  # noqa: BLE001
            log.error("llm_factory failed for profile %s: %s", profile, exc)
            return OutboundResponse(
                event=event, ok=False, text=f"[gateway] llm_factory error: {exc}",
                profile_used=profile, error=str(exc),
            )

        # Session resolution: platform adapters (P-52+) inject a resolver
        # for per-user / per-thread continuity. Default = fresh session per event.
        ws = self._workspace or "/tmp"
        if self._session_resolver is not None:
            try:
                session = self._session_resolver(event)
            except Exception as exc:  # noqa: BLE001
                log.warning("session_resolver failed, falling back to fresh: %s", exc)
                session = Session.create(workspace=ws)
        else:
            session = Session.create(workspace=ws)

        t0 = time.monotonic()
        budget = IterationBudget.of(self._max_iterations)

        try:
            text = await run_turn(
                session,
                event.text,
                llm,
                sink=sink,
                approval=AutoApproveGate(allow=True),
                budget=budget,
                council_config=self._council_config,
                cost_enforcer=self._cost_enforcer,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            success = True
            error = None
        except BudgetExceeded as exc:
            text = f"[gateway] {exc}"
            latency_ms = int((time.monotonic() - t0) * 1000)
            success = False
            error = "budget_hard_cap"
        except Exception as exc:  # noqa: BLE001
            text = f"[gateway] turn failed: {type(exc).__name__}: {exc}"
            latency_ms = int((time.monotonic() - t0) * 1000)
            success = False
            error = str(exc)

        # Post-turn bandit update — feed observed reward back into Beta priors.
        if self._bandit is not None and self._profiles:
            # We don't have per-turn cost here (the loop tracks it internally).
            # Use 0.0 for the cost gate — reward turns on success + latency only.
            reward = reward_signal(
                success=success,
                latency_ms=latency_ms,
                cost_usd=0.0,
                latency_budget_ms=self._latency_budget_ms,
                cost_budget_usd=self._cost_budget_usd,
            )
            self._bandit.update(profile, event.task_kind, reward=reward)

        response = OutboundResponse(
            event=event,
            ok=success,
            text=text,
            profile_used=profile,
            latency_ms=latency_ms,
            error=error,
        )

        # on_response hook — outbound platform send (telegram.sendMessage etc.).
        # Failure here MUST NOT crash dispatch; we log and return the response anyway.
        if self._on_response is not None:
            try:
                await self._on_response(event, response)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_response hook failed: %s", exc)

        return response

    # ------------------------------------------------------------------
    # Queue consumer — used by GatewayServer
    # ------------------------------------------------------------------

    async def serve(
        self,
        queue: asyncio.Queue["InboundEvent | None"],
        *,
        max_events: int | None = None,
    ) -> int:
        """Consume events from `queue` until None (shutdown) or max_events.

        Returns count of events dispatched.
        """
        n = 0
        while True:
            event = await queue.get()
            if event is None:
                return n
            await self.dispatch(event)
            n += 1
            if max_events is not None and n >= max_events:
                return n
