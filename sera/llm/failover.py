"""Provider failover chain with typed FailoverReason.

Wraps a list of LLM adapters. On any provider error (429, 5xx, timeout, auth)
the chain rotates to the next adapter transparently. ContextOverflow is never
swallowed — the agent loop owns that path.

Typed reasons: RateLimit | Quota | ServerError | Timeout | AuthExpired | Unknown
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sera.llm.base import ContextOverflow, LLM, StreamChunk


# ---------------------------------------------------------------------------
# Reason taxonomy
# ---------------------------------------------------------------------------

class FailoverReason(enum.Enum):
    RateLimit = "RateLimit"      # HTTP 429
    Quota = "Quota"              # billing / quota exhausted
    ServerError = "ServerError"  # 5xx, overloaded
    Timeout = "Timeout"          # network / read timeout
    AuthExpired = "AuthExpired"  # 401 / 403
    Unknown = "Unknown"          # anything else worth failing over


# ---------------------------------------------------------------------------
# Event log entry
# ---------------------------------------------------------------------------

@dataclass
class FailoverEvent:
    primary: str          # "provider/model" of the adapter that failed
    reason: FailoverReason
    error: str            # str(exc) — trimmed to 200 chars
    fallback: str | None  # "provider/model" of next adapter, None if exhausted
    recorded_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Exception classifier
# ---------------------------------------------------------------------------

def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across SDK versions."""
    for attr in ("status_code", "status", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    # openai SDK: exc.response.status_code
    resp = getattr(exc, "response", None)
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if isinstance(val, int):
                return val
    # anthropic SDK: exc.status_code inside body
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        val = body.get("status") or body.get("status_code")
        if isinstance(val, int):
            return val
    return None


def classify(exc: BaseException) -> FailoverReason:
    """Map a provider exception to a FailoverReason."""
    status = _status_code(exc)
    msg = str(exc).lower()

    if status == 429 or "rate limit" in msg or "rate_limit" in msg or "ratelimit" in msg or "too many requests" in msg:
        return FailoverReason.RateLimit

    if status == 402 or "quota" in msg or "insufficient_quota" in msg or "billing" in msg or "credit" in msg:
        return FailoverReason.Quota

    if status in (401, 403) or "unauthorized" in msg or "invalid api key" in msg or "api_key_invalid" in msg or "authentication" in msg:
        return FailoverReason.AuthExpired

    if status in (500, 502, 503, 504, 529) or "server error" in msg or "overloaded" in msg or "internal error" in msg or "bad gateway" in msg:
        return FailoverReason.ServerError

    if "timeout" in msg or "timed out" in msg or "read timeout" in msg or "connect timeout" in msg:
        return FailoverReason.Timeout

    return FailoverReason.Unknown


# ---------------------------------------------------------------------------
# FailoverChain
# ---------------------------------------------------------------------------

def _adapter_label(llm: LLM) -> str:
    return f"{llm.name}/{getattr(llm, 'model', '?')}"


class FailoverChain:
    """LLM-compatible adapter that rotates through a list of providers on error.

    Plug in wherever an LLM is expected — the chain is transparent to run_turn.
    ContextOverflow is always re-raised; all other provider errors trigger failover.
    """

    def __init__(self, llms: list[LLM]) -> None:
        if not llms:
            raise ValueError("FailoverChain requires at least one LLM")
        self._llms = list(llms)
        self._events: list[FailoverEvent] = []

    # LLM protocol surface
    @property
    def name(self) -> str:
        return self._llms[0].name

    @property
    def model(self) -> str:
        return getattr(self._llms[0], "model", "?")

    @property
    def context_budget(self) -> int:
        return self._llms[0].context_budget

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        for idx, llm in enumerate(self._llms):
            try:
                async for chunk in llm.stream(messages, tools=tools, system=system):
                    yield chunk
                return  # success — stop iterating adapters
            except ContextOverflow:
                raise  # agent loop owns this
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — intentional catch-all for failover
                reason = classify(exc)
                next_llm = self._llms[idx + 1] if idx + 1 < len(self._llms) else None
                self._events.append(
                    FailoverEvent(
                        primary=_adapter_label(llm),
                        reason=reason,
                        error=str(exc)[:200],
                        fallback=_adapter_label(next_llm) if next_llm else None,
                    )
                )
                if next_llm is None:
                    raise  # exhausted — propagate last error
                # continue loop → try next adapter

    # Inspection

    def events(self) -> list[FailoverEvent]:
        """Return all recorded failover events (newest last)."""
        return list(self._events)

    def last_event(self) -> FailoverEvent | None:
        return self._events[-1] if self._events else None
