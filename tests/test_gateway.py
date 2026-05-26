"""Tests for sera.gateway.{router, server} — webhook receiver + agent-aware router.

P-51 outclass: gateway routes inbound events through bandit + budget + council
before invoking run_turn. Verification covers each primitive.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, AsyncIterator
from pathlib import Path


from sera.gateway.router import InboundEvent, Router
from sera.gateway.server import build_server, default_parser
from sera.llm.bandit import ThompsonBandit
from sera.llm.base import StreamChunk
from sera.llm.budget import BudgetConfig, BudgetEnforcer


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubLLM:
    """Single-shot text reply; track invocations."""
    name = "openai"  # ReAct loop uses the OpenAI branch for tool schemas
    context_budget = 32_000

    def __init__(self, *, reply: str = "ok", model: str = "stub") -> None:
        self._reply = reply
        self.model = model
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk(delta_text=self._reply)
        yield StreamChunk(finish_reason="stop", usage={"input_tokens": 10, "output_tokens": 5})


def _event(text: str = "hello", platform: str = "telegram") -> InboundEvent:
    return InboundEvent(
        platform=platform,
        user_id="u1",
        channel_id="c1",
        text=text,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# InboundEvent
# ---------------------------------------------------------------------------

class TestInboundEvent:
    def test_task_kind_default_chat(self) -> None:
        assert _event("hello").task_kind == "chat"

    def test_task_kind_slash_command(self) -> None:
        assert _event("/help").task_kind == "command"

    def test_task_kind_metadata_override(self) -> None:
        e = InboundEvent(
            platform="x", user_id="u", channel_id="c", text="hi",
            metadata={"task_kind": "summarize"},
        )
        assert e.task_kind == "summarize"


# ---------------------------------------------------------------------------
# Router.dispatch — minimum viable path
# ---------------------------------------------------------------------------

class TestDispatchMinimal:
    def test_default_path_calls_llm(self) -> None:
        llm = _StubLLM(reply="echo: hi")
        router = Router(llm_factory=lambda _p: llm)

        async def _go():
            return await router.dispatch(_event("hi"))

        resp = _run(_go())
        assert resp.ok
        assert "echo: hi" in resp.text
        assert llm.calls >= 1
        assert resp.profile_used == "default"

    def test_response_records_latency(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())
        resp = _run(router.dispatch(_event()))
        assert resp.latency_ms >= 0
        assert resp.error is None

    def test_handled_counter(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())
        _run(router.dispatch(_event()))
        _run(router.dispatch(_event()))
        assert router.n_handled == 2


# ---------------------------------------------------------------------------
# Outclass 1: bandit-routed dispatch
# ---------------------------------------------------------------------------

class TestBanditRouting:
    """Router calls Thompson bandit per event and updates arms post-turn."""

    def test_bandit_picks_profile(self) -> None:
        import random
        bandit = ThompsonBandit(rng=random.Random(0))
        seen_profiles: list[str] = []

        def factory(profile: str):
            seen_profiles.append(profile)
            return _StubLLM(model=profile)

        router = Router(
            llm_factory=factory,
            profiles=["cheap", "big"],
            bandit=bandit,
        )
        _run(router.dispatch(_event("hello")))
        assert seen_profiles[0] in {"cheap", "big"}

    def test_bandit_updates_post_turn(self) -> None:
        import random
        bandit = ThompsonBandit(rng=random.Random(0))
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            profiles=["cheap", "big"],
            bandit=bandit,
        )
        _run(router.dispatch(_event("hi")))
        # At least one arm should have been touched (alpha or beta moved)
        state = bandit.state()
        assert len(state) >= 1
        arm = list(state.values())[0]
        assert arm["n"] >= 1

    def test_no_bandit_no_arm_state(self) -> None:
        # Without bandit configured, arm state stays empty.
        router = Router(llm_factory=lambda _p: _StubLLM())
        _run(router.dispatch(_event()))
        # No bandit means we just don't track arms; test mostly proves it doesn't crash.
        assert router.n_handled == 1


# ---------------------------------------------------------------------------
# Outclass 2: cost-enforced dispatch
# ---------------------------------------------------------------------------

class TestCostEnforcement:
    def test_hard_block_returns_refusal_without_llm_call(self) -> None:
        cfg = BudgetConfig(session_soft_usd=0.01, session_hard_usd=0.01)
        enforcer = BudgetEnforcer(cfg, _db=Path("/nonexistent/db.db"))
        enforcer.add(1.00)  # push session spend over hard cap

        llm = _StubLLM()
        router = Router(llm_factory=lambda _p: llm, cost_enforcer=enforcer)
        resp = _run(router.dispatch(_event()))

        assert not resp.ok
        assert resp.blocked_by_budget
        assert resp.error == "budget_hard_cap"
        assert llm.calls == 0  # never reached the model

    def test_soft_warning_still_runs(self) -> None:
        cfg = BudgetConfig(session_soft_usd=0.001, session_hard_usd=10.0)
        enforcer = BudgetEnforcer(cfg, _db=Path("/nonexistent/db.db"))
        enforcer.add(0.005)  # past soft, below hard

        llm = _StubLLM(reply="still running")
        router = Router(llm_factory=lambda _p: llm, cost_enforcer=enforcer)
        resp = _run(router.dispatch(_event()))

        assert resp.ok
        assert llm.calls >= 1

    def test_no_enforcer_no_block(self) -> None:
        llm = _StubLLM()
        router = Router(llm_factory=lambda _p: llm)
        resp = _run(router.dispatch(_event()))
        assert resp.ok
        assert not resp.blocked_by_budget


# ---------------------------------------------------------------------------
# Cost plumbing: run_turn stamps actual cost → router feeds bandit reward gate
# ---------------------------------------------------------------------------

class _CostedLLM:
    """Stub that reports token usage under a priced model so _turn_cost > 0."""
    name = "openai"
    context_budget = 32_000

    def __init__(self, *, model: str = "claude-opus-4-7", in_tok: int = 100_000, out_tok: int = 100_000) -> None:
        self.model = model
        self._in = in_tok
        self._out = out_tok
        self.calls = 0

    async def stream(self, messages, tools=None, system=None):
        self.calls += 1
        yield StreamChunk(delta_text="done")
        yield StreamChunk(
            finish_reason="stop",
            usage={"input_tokens": self._in, "output_tokens": self._out},
        )


class TestCostPlumbing:
    def test_run_turn_stamps_actual_cost_on_session(self, tmp_path) -> None:
        """The session the router dispatched into carries the real turn cost."""
        from sera.memory.session import Session

        session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")
        router = Router(
            llm_factory=lambda _p: _CostedLLM(),
            session_resolver=lambda _e: session,
        )
        resp = _run(router.dispatch(_event("hi")))
        assert resp.ok
        # opus pricing on 100k+100k tokens is non-trivial; plumbing proven if > 0.
        assert session.last_turn_cost_usd > 0.0

    def test_cost_over_budget_zeroes_reward(self, tmp_path) -> None:
        """A turn whose real cost exceeds cost_budget_usd earns reward 0 (beta moves)."""
        import random
        from sera.memory.session import Session

        session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")
        bandit = ThompsonBandit(rng=random.Random(0))
        router = Router(
            llm_factory=lambda _p: _CostedLLM(),  # ~$9 turn at opus pricing
            profiles=["cheap", "big"],
            bandit=bandit,
            session_resolver=lambda _e: session,
            cost_budget_usd=0.0001,                # far below the turn's real cost
        )
        _run(router.dispatch(_event("hi")))
        # Reward gate failed on cost → the picked arm's beta incremented, alpha did not.
        arm = next(iter(bandit.state().values()))
        assert arm["beta"] > arm["alpha"], f"cost gate did not zero reward: {arm}"

    def test_cheap_turn_under_budget_earns_reward(self, tmp_path) -> None:
        """The same turn under a generous cost budget earns reward 1 (alpha moves)."""
        import random
        from sera.memory.session import Session

        session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "s.db")
        bandit = ThompsonBandit(rng=random.Random(0))
        router = Router(
            llm_factory=lambda _p: _CostedLLM(),
            profiles=["cheap", "big"],
            bandit=bandit,
            session_resolver=lambda _e: session,
            cost_budget_usd=1000.0,                # generous — cost gate passes
        )
        _run(router.dispatch(_event("hi")))
        arm = next(iter(bandit.state().values()))
        assert arm["alpha"] > arm["beta"], f"reward not granted under budget: {arm}"


# ---------------------------------------------------------------------------
# Outclass 3: memory ingest hook
# ---------------------------------------------------------------------------

class TestMemoryIngest:
    def test_on_inbound_fires_before_dispatch(self) -> None:
        captured: list[InboundEvent] = []

        async def _ingest(event: InboundEvent) -> None:
            captured.append(event)

        router = Router(llm_factory=lambda _p: _StubLLM(), on_inbound=_ingest)
        _run(router.dispatch(_event("track this")))
        assert len(captured) == 1
        assert captured[0].text == "track this"

    def test_ingest_failure_does_not_block_dispatch(self) -> None:
        async def _broken(event: InboundEvent) -> None:
            raise RuntimeError("ingest down")

        router = Router(llm_factory=lambda _p: _StubLLM(), on_inbound=_broken)
        resp = _run(router.dispatch(_event()))
        assert resp.ok  # ingest hook failure must not block the turn


# ---------------------------------------------------------------------------
# Router.serve — queue consumer
# ---------------------------------------------------------------------------

class TestRouterServe:
    def test_serve_consumes_until_sentinel(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())

        async def _go():
            queue: asyncio.Queue = asyncio.Queue()
            for i in range(3):
                queue.put_nowait(_event(f"msg {i}"))
            queue.put_nowait(None)  # shutdown sentinel
            return await router.serve(queue)

        n = _run(_go())
        assert n == 3
        assert router.n_handled == 3

    def test_serve_max_events(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())

        async def _go():
            queue: asyncio.Queue = asyncio.Queue()
            for i in range(10):
                queue.put_nowait(_event(f"msg {i}"))
            return await router.serve(queue, max_events=4)

        n = _run(_go())
        assert n == 4


# ---------------------------------------------------------------------------
# default_parser
# ---------------------------------------------------------------------------

class TestDefaultParser:
    def test_text_key(self) -> None:
        e = default_parser("telegram", {"text": "hi", "user_id": "u1", "channel_id": "c1"})
        assert e is not None
        assert e.text == "hi"
        assert e.platform == "telegram"

    def test_content_key_for_discord(self) -> None:
        e = default_parser("discord", {"content": "hello", "user": "u2"})
        assert e is not None
        assert e.text == "hello"
        assert e.user_id == "u2"

    def test_chat_id_for_telegram(self) -> None:
        e = default_parser("telegram", {"text": "hi", "from": "u3", "chat_id": "ch9"})
        assert e is not None
        assert e.channel_id == "ch9"
        assert e.user_id == "u3"

    def test_empty_text_returns_none(self) -> None:
        assert default_parser("x", {"user_id": "u"}) is None

    def test_no_keys_returns_none(self) -> None:
        assert default_parser("x", {}) is None


# ---------------------------------------------------------------------------
# GatewayServer — HTTP layer (P-51 verification: curl-able webhook)
# ---------------------------------------------------------------------------

def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class TestGatewayServer:
    def test_healthz(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                code, body = _get(f"{server.url}/healthz")
                assert code == 200
                assert body == {"ok": True}
            finally:
                server.stop()

        _run(_go())

    def test_webhook_enqueues_event(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                code, body = _post(
                    f"{server.url}/webhook/telegram",
                    {"text": "hello", "user_id": "u1", "chat_id": "c1"},
                )
                assert code == 202
                assert body["accepted"] is True
                # Pull from the queue
                event = await asyncio.wait_for(queue.get(), timeout=2.0)
                assert event.platform == "telegram"
                assert event.text == "hello"
                assert event.user_id == "u1"
                assert event.channel_id == "c1"
            finally:
                server.stop()

        _run(_go())

    def test_curl_able_three_platforms(self) -> None:
        """P-51 verification: curl-able webhook on multiple platforms."""
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                platforms = ["telegram", "discord", "slack"]
                for p in platforms:
                    code, _ = _post(
                        f"{server.url}/webhook/{p}",
                        {"text": f"hi from {p}", "user_id": "u", "channel_id": "c"},
                    )
                    assert code == 202
                # All three landed in the queue
                events = []
                for _ in platforms:
                    events.append(await asyncio.wait_for(queue.get(), timeout=2.0))
                assert {e.platform for e in events} == set(platforms)
            finally:
                server.stop()

        _run(_go())

    def test_bad_json_returns_400(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                req = urllib.request.Request(
                    f"{server.url}/webhook/x",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=3.0)
                    assert False, "should have raised"
                except urllib.error.HTTPError as e:
                    assert e.code == 400
            finally:
                server.stop()

        _run(_go())

    def test_unparseable_payload_returns_422(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                code, body = _post(f"{server.url}/webhook/x", {"only_metadata": True})
                assert code == 422
            finally:
                server.stop()

        _run(_go())

    def test_unknown_path_returns_404(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                code, _ = _get(f"{server.url}/nope")
                assert code == 404
            finally:
                server.stop()

        _run(_go())

    def test_stats_endpoint(self) -> None:
        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                _post(f"{server.url}/webhook/x", {"text": "hi", "user_id": "u", "channel_id": "c"})
                code, body = _get(f"{server.url}/stats")
                assert code == 200
                assert body["accepted"] >= 1
            finally:
                server.stop()

        _run(_go())


# ---------------------------------------------------------------------------
# End-to-end: HTTP POST → Router consumes → bandit updates
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_http_post_drives_bandit_update(self) -> None:
        """HTTP POST → queue → Router.dispatch → bandit arm updates."""
        import random
        bandit = ThompsonBandit(rng=random.Random(0))

        async def _go():
            server, queue = build_server(port=0)
            server.start()
            try:
                # Drop 3 events via HTTP. The server's worker thread schedules
                # call_soon_threadsafe; the loop only processes those on the
                # next tick — so yield via asyncio.sleep before consuming.
                for i in range(3):
                    code, _ = _post(
                        f"{server.url}/webhook/telegram",
                        {"text": f"msg {i}", "user_id": "u", "channel_id": "c"},
                    )
                    assert code == 202

                # Let the loop drain the scheduled put_nowait callbacks.
                await asyncio.sleep(0.05)

                router = Router(
                    llm_factory=lambda _p: _StubLLM(),
                    profiles=["cheap", "big"],
                    bandit=bandit,
                )
                # max_events terminates cleanly without a sentinel race.
                return await router.serve(queue, max_events=3)
            finally:
                server.stop()

        n = _run(_go())
        assert n == 3
        state = bandit.state()
        total_n = sum(arm["n"] for arm in state.values())
        assert total_n >= 3, f"bandit not updated end-to-end: {state}"
