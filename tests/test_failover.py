"""Tests for sera.llm.failover — FailoverReason, classify, FailoverChain."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from sera.llm.base import ContextOverflow, StreamChunk
from sera.llm.failover import (
    FailoverChain,
    FailoverEvent,
    FailoverReason,
    classify,
)


# ---------------------------------------------------------------------------
# Helpers — stub LLM adapters
# ---------------------------------------------------------------------------

class _OkLLM:
    """Always succeeds, yields one text chunk."""
    name = "ok"
    model = "ok-model"
    context_budget = 128_000

    async def stream(self, messages, *, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="ok")
        yield StreamChunk(finish_reason="stop")


class _FailLLM:
    """Always raises the given exception."""
    name = "fail"
    model = "fail-model"
    context_budget = 128_000

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def stream(self, messages, *, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        raise self._exc
        yield  # make it an async generator  # noqa: unreachable


class _CountLLM:
    """Counts how many times stream was called."""
    name = "count"
    model = "count-model"
    context_budget = 128_000
    calls: int = 0

    async def stream(self, messages, *, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        _CountLLM.calls += 1
        yield StreamChunk(delta_text="counted")
        yield StreamChunk(finish_reason="stop")


def _exc_with_status(status: int, msg: str = "") -> Exception:
    exc = RuntimeError(msg or f"HTTP {status}")
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


async def _collect(chain: FailoverChain, messages=None) -> list[StreamChunk]:
    chunks: list[StreamChunk] = []
    async for chunk in chain.stream(messages or [], tools=None, system=None):
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

class TestClassify:
    def test_429_status(self) -> None:
        assert classify(_exc_with_status(429)) == FailoverReason.RateLimit

    def test_rate_limit_message(self) -> None:
        assert classify(RuntimeError("rate limit exceeded")) == FailoverReason.RateLimit

    def test_too_many_requests(self) -> None:
        assert classify(RuntimeError("too many requests")) == FailoverReason.RateLimit

    def test_quota_message(self) -> None:
        assert classify(RuntimeError("insufficient_quota")) == FailoverReason.Quota

    def test_402_status(self) -> None:
        assert classify(_exc_with_status(402)) == FailoverReason.Quota

    def test_billing_message(self) -> None:
        assert classify(RuntimeError("billing hard limit reached")) == FailoverReason.Quota

    def test_401_auth(self) -> None:
        assert classify(_exc_with_status(401)) == FailoverReason.AuthExpired

    def test_403_auth(self) -> None:
        assert classify(_exc_with_status(403)) == FailoverReason.AuthExpired

    def test_invalid_api_key(self) -> None:
        assert classify(RuntimeError("invalid api key provided")) == FailoverReason.AuthExpired

    def test_500_server(self) -> None:
        assert classify(_exc_with_status(500)) == FailoverReason.ServerError

    def test_529_overloaded(self) -> None:
        assert classify(_exc_with_status(529)) == FailoverReason.ServerError

    def test_overloaded_message(self) -> None:
        assert classify(RuntimeError("Anthropic is currently overloaded")) == FailoverReason.ServerError

    def test_timeout_message(self) -> None:
        assert classify(RuntimeError("read timeout")) == FailoverReason.Timeout

    def test_timed_out(self) -> None:
        assert classify(RuntimeError("connection timed out")) == FailoverReason.Timeout

    def test_unknown(self) -> None:
        assert classify(RuntimeError("unexpected weirdness")) == FailoverReason.Unknown


# ---------------------------------------------------------------------------
# FailoverChain — construction
# ---------------------------------------------------------------------------

class TestChainInit:
    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            FailoverChain([])

    def test_name_from_primary(self) -> None:
        chain = FailoverChain([_OkLLM()])
        assert chain.name == "ok"

    def test_budget_from_primary(self) -> None:
        chain = FailoverChain([_OkLLM()])
        assert chain.context_budget == 128_000


# ---------------------------------------------------------------------------
# FailoverChain — success path
# ---------------------------------------------------------------------------

class TestChainSuccess:
    def test_no_events_on_success(self) -> None:
        chain = FailoverChain([_OkLLM()])
        asyncio.run(_collect(chain))
        assert chain.events() == []

    def test_yields_chunks(self) -> None:
        chain = FailoverChain([_OkLLM()])
        chunks = asyncio.run(_collect(chain))
        texts = [c.delta_text for c in chunks if c.delta_text]
        assert "ok" in texts


# ---------------------------------------------------------------------------
# FailoverChain — 429 → fallback (P-38 verification criterion)
# ---------------------------------------------------------------------------

class TestFailover429:
    def _rate_limit_exc(self) -> Exception:
        exc = RuntimeError("rate limit exceeded")
        exc.status_code = 429  # type: ignore[attr-defined]
        return exc

    def test_failover_on_429(self) -> None:
        """Simulated 429 → fallback path observed in events trace."""
        chain = FailoverChain([_FailLLM(self._rate_limit_exc()), _OkLLM()])
        asyncio.run(_collect(chain))
        assert len(chain.events()) == 1
        ev = chain.events()[0]
        assert ev.reason == FailoverReason.RateLimit
        assert ev.primary == "fail/fail-model"
        assert ev.fallback == "ok/ok-model"

    def test_failover_yields_fallback_chunks(self) -> None:
        chain = FailoverChain([_FailLLM(self._rate_limit_exc()), _OkLLM()])
        chunks = asyncio.run(_collect(chain))
        texts = [c.delta_text for c in chunks if c.delta_text]
        assert "ok" in texts

    def test_last_event(self) -> None:
        chain = FailoverChain([_FailLLM(self._rate_limit_exc()), _OkLLM()])
        asyncio.run(_collect(chain))
        ev = chain.last_event()
        assert ev is not None
        assert ev.reason == FailoverReason.RateLimit

    def test_event_has_timestamp(self) -> None:
        import time
        chain = FailoverChain([_FailLLM(self._rate_limit_exc()), _OkLLM()])
        t0 = time.time()
        asyncio.run(_collect(chain))
        assert chain.events()[0].recorded_at >= t0

    def test_event_error_truncated(self) -> None:
        long_msg = "x" * 300
        exc = RuntimeError(long_msg)
        exc.status_code = 429  # type: ignore[attr-defined]
        chain = FailoverChain([_FailLLM(exc), _OkLLM()])
        asyncio.run(_collect(chain))
        assert len(chain.events()[0].error) <= 200


# ---------------------------------------------------------------------------
# FailoverChain — multiple failovers
# ---------------------------------------------------------------------------

class TestMultiFailover:
    def test_two_failures_then_success(self) -> None:
        exc = _exc_with_status(503, "server error")
        chain = FailoverChain([_FailLLM(exc), _FailLLM(exc), _OkLLM()])
        asyncio.run(_collect(chain))
        assert len(chain.events()) == 2
        assert all(e.reason == FailoverReason.ServerError for e in chain.events())

    def test_all_exhausted_raises(self) -> None:
        exc = _exc_with_status(429)
        chain = FailoverChain([_FailLLM(exc), _FailLLM(exc)])
        with pytest.raises(Exception):
            asyncio.run(_collect(chain))
        assert len(chain.events()) == 2
        assert chain.last_event().fallback is None  # exhausted

    def test_exhausted_event_no_fallback(self) -> None:
        exc = _exc_with_status(429)
        chain = FailoverChain([_FailLLM(exc)])
        with pytest.raises(Exception):
            asyncio.run(_collect(chain))
        assert chain.last_event().fallback is None


# ---------------------------------------------------------------------------
# FailoverChain — ContextOverflow passthrough
# ---------------------------------------------------------------------------

class TestContextOverflowPassthrough:
    def test_context_overflow_not_swallowed(self) -> None:
        chain = FailoverChain([_FailLLM(ContextOverflow("too long")), _OkLLM()])
        with pytest.raises(ContextOverflow):
            asyncio.run(_collect(chain))
        # ContextOverflow must NOT trigger failover
        assert chain.events() == []
