"""Tests for SSE streaming on the Sera HTTP API (P-62).

Outclass verified:
- Glass-box streaming: tool_start/tool_end events stream alongside tokens
- First token arrives BEFORE the turn completes (real streaming, not buffered)
- Zero-dependency SSE framing (event:/data:) parses correctly
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import AsyncIterator

import pytest

from sera.gateway.router import Router
from sera.llm.base import StreamChunk
from sera.rpc.http_api import (
    SeraHTTPAPI,
    SignedBearer,
    make_async_bridge,
    make_streaming_bridge,
)


# ---------------------------------------------------------------------------
# Stub LLMs
# ---------------------------------------------------------------------------

class _TokenLLM:
    """Yields three text deltas — exercises the token stream."""
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        for piece in ["Hel", "lo ", "world"]:
            yield StreamChunk(delta_text=piece)
        yield StreamChunk(finish_reason="stop")


def _loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=3.0)
    loop.close()


# ---------------------------------------------------------------------------
# Streaming bridge — direct (no socket)
# ---------------------------------------------------------------------------

class TestStreamingBridge:
    def test_tokens_then_done(self) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _TokenLLM())
            stream = make_streaming_bridge(loop, router)
            events = list(stream({"text": "hi"}))
            kinds = [e[0] for e in events]
            assert "token" in kinds
            assert kinds[-1] == "done"
        finally:
            _stop_loop(loop, t)

    def test_tokens_in_order(self) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _TokenLLM())
            stream = make_streaming_bridge(loop, router)
            tokens = [json.loads(d)["text"] for k, d in stream({"text": "hi"}) if k == "token"]
            assert "".join(tokens).strip() == "Hello world"
        finally:
            _stop_loop(loop, t)

    def test_done_carries_full_text(self) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _TokenLLM())
            stream = make_streaming_bridge(loop, router)
            events = list(stream({"text": "hi"}))
            done = [json.loads(d) for k, d in events if k == "done"][0]
            assert done["ok"] is True
            assert "Hello world" in done["text"]
        finally:
            _stop_loop(loop, t)

    def test_empty_text_raises(self) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _TokenLLM())
            stream = make_streaming_bridge(loop, router)
            with pytest.raises(ValueError):
                list(stream({"text": ""}))
        finally:
            _stop_loop(loop, t)


# ---------------------------------------------------------------------------
# Glass-box: tool trace streams alongside tokens
# ---------------------------------------------------------------------------

class _ToolLLM:
    """First turn calls a tool; second turn answers. Exercises tool events."""
    name = "openai"
    context_budget = 32_000
    model = "stub"

    def __init__(self) -> None:
        self._calls = 0

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        self._calls += 1
        if self._calls == 1:
            yield StreamChunk(
                tool_call_delta={
                    "id": "c1",
                    "name": "file_read",
                    "arguments": {"path": "/tmp/nope"},
                },
                finish_reason="tool_calls",
            )
        else:
            yield StreamChunk(delta_text="done reading")
            yield StreamChunk(finish_reason="stop")


class TestGlassBox:
    def test_tool_events_stream(self, tmp_path) -> None:
        """The outclass: tool_start/tool_end events appear in the stream."""
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _ToolLLM(), workspace=str(tmp_path))
            stream = make_streaming_bridge(loop, router)
            kinds = [k for k, _ in stream({"text": "read the file"})]
            assert "tool_start" in kinds, "live tool trace must stream (glass-box outclass)"
            assert "tool_end" in kinds
            assert kinds[-1] == "done"
        finally:
            _stop_loop(loop, t)

    def test_tool_start_names_the_tool(self, tmp_path) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _ToolLLM(), workspace=str(tmp_path))
            stream = make_streaming_bridge(loop, router)
            starts = [json.loads(d)["name"] for k, d in stream({"text": "go"}) if k == "tool_start"]
            assert "file_read" in starts
        finally:
            _stop_loop(loop, t)


# ---------------------------------------------------------------------------
# First-token-before-completion — the real proof streaming streams
# ---------------------------------------------------------------------------

class _SlowTailLLM:
    """Emits a token immediately, then stalls before finishing.

    Proves the first token reaches the client well before the turn completes —
    the property the blueprint's '<100ms first token' depends on.
    """
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="FIRST")
        await asyncio.sleep(0.5)            # stall after the first token
        yield StreamChunk(delta_text="LAST")
        yield StreamChunk(finish_reason="stop")


class TestFirstTokenLatency:
    def test_first_token_arrives_before_turn_done(self) -> None:
        loop, t = _loop_in_thread()
        try:
            router = Router(llm_factory=lambda _p: _SlowTailLLM())
            stream = make_streaming_bridge(loop, router)
            gen = stream({"text": "hi"})

            t0 = time.monotonic()
            # Pull events until the first token.
            first_token_at = None
            done_at = None
            for kind, _data in gen:
                now = time.monotonic() - t0
                if kind == "token" and first_token_at is None:
                    first_token_at = now
                if kind == "done":
                    done_at = now
            assert first_token_at is not None
            assert done_at is not None
            # The first token must land clearly before completion (LLM stalls 0.5s).
            assert done_at - first_token_at >= 0.4, (
                "first token should arrive long before the turn finishes"
            )
        finally:
            _stop_loop(loop, t)


# ---------------------------------------------------------------------------
# E2E: real socket, SSE over HTTP
# ---------------------------------------------------------------------------

class TestE2ESSE:
    def _read_sse(self, url: str, token: str, body: dict) -> list[tuple[str, str]]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        events: list[tuple[str, str]] = []
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            assert resp.headers.get("Content-Type") == "text/event-stream"
            buf = ""
            raw = resp.read().decode("utf-8")
            buf += raw
            for frame in buf.split("\n\n"):
                if not frame.strip():
                    continue
                ev, data = "message", ""
                for line in frame.split("\n"):
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                events.append((ev, data))
        return events

    def test_sse_round_trip(self) -> None:
        loop, t = _loop_in_thread()
        router = Router(llm_factory=lambda _p: _TokenLLM())
        bearer = SignedBearer(signing_key="k")
        api = SeraHTTPAPI(
            host="127.0.0.1", port=0,
            turn_fn=make_async_bridge(loop, router),
            stream_fn=make_streaming_bridge(loop, router),
            bearer=bearer,
        )
        api.start()
        try:
            token = bearer.issue("cli", scopes=["turn"])
            events = self._read_sse(f"{api.url}/v1/turn/stream", token, {"text": "hi"})
            kinds = [e[0] for e in events]
            assert "token" in kinds
            assert kinds[-1] == "done"
            tokens = [json.loads(d)["text"] for k, d in events if k == "token"]
            assert "".join(tokens).strip() == "Hello world"
        finally:
            api.stop()
            _stop_loop(loop, t)

    def test_stream_requires_auth(self) -> None:
        loop, t = _loop_in_thread()
        router = Router(llm_factory=lambda _p: _TokenLLM())
        bearer = SignedBearer(signing_key="k")
        api = SeraHTTPAPI(
            host="127.0.0.1", port=0,
            turn_fn=make_async_bridge(loop, router),
            stream_fn=make_streaming_bridge(loop, router),
            bearer=bearer,
        )
        api.start()
        try:
            req = urllib.request.Request(
                f"{api.url}/v1/turn/stream",
                data=json.dumps({"text": "hi"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5.0)
                status = 200
            except urllib.error.HTTPError as e:
                status = e.code
            assert status == 401
        finally:
            api.stop()
            _stop_loop(loop, t)

    def test_stream_501_when_not_enabled(self) -> None:
        loop, t = _loop_in_thread()
        router = Router(llm_factory=lambda _p: _TokenLLM())
        bearer = SignedBearer(signing_key="k")
        api = SeraHTTPAPI(
            host="127.0.0.1", port=0,
            turn_fn=make_async_bridge(loop, router),
            bearer=bearer,            # no stream_fn
        )
        api.start()
        try:
            token = bearer.issue("cli", scopes=["turn"])
            req = urllib.request.Request(
                f"{api.url}/v1/turn/stream",
                data=json.dumps({"text": "hi"}).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5.0)
                status = 200
            except urllib.error.HTTPError as e:
                status = e.code
            assert status == 501
        finally:
            api.stop()
            _stop_loop(loop, t)

    def test_openapi_documents_stream(self) -> None:
        from sera.rpc.http_api import build_openapi_spec
        spec = build_openapi_spec()
        assert "/v1/turn/stream" in spec["paths"]
        assert spec["paths"]["/v1/turn/stream"]["post"]["security"] == [{"bearerAuth": []}]
