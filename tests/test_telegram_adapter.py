"""Tests for sera.gateway.platforms.telegram — parser + sender + 24h continuity.

P-52 verification: message → reply, 24h gap preserves session.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from sera.gateway.platforms.telegram import (
    DEFAULT_SESSION_TTL_S,
    TelegramSendResult,
    TelegramSender,
    TelegramSessionStore,
    parse_telegram,
)
from sera.gateway.router import InboundEvent, OutboundResponse, Router
from sera.llm.base import StreamChunk


# ---------------------------------------------------------------------------
# parse_telegram
# ---------------------------------------------------------------------------

def _tg_update(
    *, user_id: int = 42, chat_id: int = 42, text: str = "hello",
    message_id: int = 100, edited: bool = False, username: str = "alice",
) -> dict:
    msg = {
        "message_id": message_id,
        "from": {"id": user_id, "username": username},
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
        "date": 1_700_000_000,
    }
    return {"update_id": 1, "edited_message" if edited else "message": msg}


class TestParser:
    def test_basic_message(self) -> None:
        e = parse_telegram(_tg_update(text="hi there"))
        assert e is not None
        assert e.platform == "telegram"
        assert e.user_id == "42"
        assert e.channel_id == "42"
        assert e.text == "hi there"
        assert e.metadata["message_id"] == 100
        assert e.metadata["username"] == "alice"
        assert e.metadata["edited"] is False

    def test_edited_message(self) -> None:
        e = parse_telegram(_tg_update(text="fixed", edited=True))
        assert e is not None
        assert e.metadata["edited"] is True

    def test_no_text_returns_none(self) -> None:
        update = _tg_update()
        update["message"].pop("text")
        assert parse_telegram(update) is None

    def test_empty_text_returns_none(self) -> None:
        assert parse_telegram(_tg_update(text="")) is None

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_telegram(_tg_update(text="   ")) is None

    def test_no_message_key_returns_none(self) -> None:
        assert parse_telegram({"update_id": 1, "callback_query": {}}) is None

    def test_group_chat(self) -> None:
        u = _tg_update(user_id=42, chat_id=-100123)
        u["message"]["chat"]["type"] = "group"
        e = parse_telegram(u)
        assert e is not None
        assert e.user_id == "42"
        assert e.channel_id == "-100123"
        assert e.metadata["chat_type"] == "group"


# ---------------------------------------------------------------------------
# TelegramSender
# ---------------------------------------------------------------------------

class TestSender:
    def _capture_poster(self) -> tuple[list[dict], Any]:
        calls: list[dict] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            payload = json.loads(data.decode("utf-8"))
            calls.append({"url": url, "headers": headers, "body": payload})
            return 200, {
                "ok": True,
                "result": {"message_id": 999, "chat": {"id": payload["chat_id"]}, "text": payload["text"]},
            }

        return calls, poster

    def test_send_message_posts_correct_payload(self) -> None:
        calls, poster = self._capture_poster()
        sender = TelegramSender(bot_token="testtoken", _poster=poster)

        result = asyncio.run(sender.send_message(chat_id=42, text="hi"))
        assert result.ok
        assert result.message_id == 999
        assert len(calls) == 1
        assert calls[0]["url"].endswith("/bottesttoken/sendMessage")
        assert calls[0]["body"] == {"chat_id": 42, "text": "hi"}

    def test_send_with_reply_to(self) -> None:
        calls, poster = self._capture_poster()
        sender = TelegramSender(bot_token="t", _poster=poster)
        asyncio.run(sender.send_message(chat_id=1, text="x", reply_to_message_id=77))
        assert calls[0]["body"]["reply_to_message_id"] == 77

    def test_send_with_parse_mode(self) -> None:
        calls, poster = self._capture_poster()
        sender = TelegramSender(bot_token="t", _poster=poster)
        asyncio.run(sender.send_message(chat_id=1, text="**bold**", parse_mode="MarkdownV2"))
        assert calls[0]["body"]["parse_mode"] == "MarkdownV2"

    def test_api_error_returns_failed_result(self) -> None:
        def poster(url, data, headers):
            return 400, {"ok": False, "description": "chat not found"}
        sender = TelegramSender(bot_token="t", _poster=poster)
        result = asyncio.run(sender.send_message(chat_id=999, text="hi"))
        assert not result.ok
        assert "chat not found" in result.error

    def test_poster_exception_returns_failed_result(self) -> None:
        def poster(url, data, headers):
            raise ConnectionError("network down")
        sender = TelegramSender(bot_token="t", _poster=poster)
        result = asyncio.run(sender.send_message(chat_id=1, text="hi"))
        assert not result.ok
        assert "network down" in result.error

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="bot_token"):
            TelegramSender(bot_token="")

    def test_reply_hook_sends_with_message_id(self) -> None:
        calls, poster = self._capture_poster()
        sender = TelegramSender(bot_token="t", _poster=poster)

        event = InboundEvent(
            platform="telegram", user_id="42", channel_id="42", text="hi",
            metadata={"message_id": 555},
        )
        response = OutboundResponse(event=event, ok=True, text="reply text")

        asyncio.run(sender.reply_hook(event, response))
        assert len(calls) == 1
        assert calls[0]["body"]["chat_id"] == "42"
        assert calls[0]["body"]["text"] == "reply text"
        assert calls[0]["body"]["reply_to_message_id"] == 555

    def test_reply_hook_skips_empty_text(self) -> None:
        calls, poster = self._capture_poster()
        sender = TelegramSender(bot_token="t", _poster=poster)
        event = InboundEvent(platform="telegram", user_id="u", channel_id="c", text="hi")
        response = OutboundResponse(event=event, ok=True, text="")
        asyncio.run(sender.reply_hook(event, response))
        assert calls == []


# ---------------------------------------------------------------------------
# TelegramSessionStore — the P-52 outclass (24h continuity)
# ---------------------------------------------------------------------------

class TestSessionStore:
    def _store(self, tmp_path: Path, *, clock=None) -> TelegramSessionStore:
        return TelegramSessionStore(
            db=tmp_path / "tg.db",
            ttl_s=DEFAULT_SESSION_TTL_S,
            clock=clock or (lambda: 0.0),
        )

    def test_first_message_creates_session(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 1000.0)
        sess = store.get_or_create("u1")
        assert sess is not None
        assert sess.id is not None
        assert store.session_id_for("u1") == sess.id

    def test_second_message_within_ttl_same_session(self, tmp_path: Path) -> None:
        # Outclass verification: 23h gap preserves session.
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 1000.0
        s1 = store.get_or_create("u1")
        t[0] = 1000.0 + (23 * 3600)        # 23 hours later
        s2 = store.get_or_create("u1")
        assert s1.id == s2.id, "23h gap must reuse the same session"

    def test_message_after_ttl_creates_new_session(self, tmp_path: Path) -> None:
        # Outclass boundary: > 24h triggers a fresh session.
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 1000.0
        s1 = store.get_or_create("u1")
        t[0] = 1000.0 + (25 * 3600)        # 25 hours later
        s2 = store.get_or_create("u1")
        assert s1.id != s2.id, "25h gap must create a new session"

    def test_distinct_users_get_distinct_sessions(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 100.0)
        a = store.get_or_create("alice")
        b = store.get_or_create("bob")
        assert a.id != b.id

    def test_resolver_returns_session_for_event(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        resolver = store.resolver(workspace="/tmp")
        event = InboundEvent(platform="telegram", user_id="u1", channel_id="c1", text="hi")
        sess1 = resolver(event)
        sess2 = resolver(event)
        assert sess1.id == sess2.id

    def test_evict_expired_removes_old_rows(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 0.0
        store.get_or_create("u1")
        t[0] = 25 * 3600          # past TTL
        removed = store.evict_expired()
        assert removed == 1

    def test_active_count_tracks_within_ttl(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        store.get_or_create("u1")
        store.get_or_create("u2")
        assert store.active_count() == 2

    def test_session_id_for_expired_returns_none(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        store.get_or_create("u1")
        t[0] = 25 * 3600
        assert store.session_id_for("u1") is None


# ---------------------------------------------------------------------------
# Router integration — session_resolver wiring
# ---------------------------------------------------------------------------

class _StubLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="reply")
        yield StreamChunk(finish_reason="stop")


class TestRouterIntegration:
    def test_session_resolver_invoked_per_event(self, tmp_path: Path) -> None:
        store = TelegramSessionStore(db=tmp_path / "tg.db", clock=lambda: 100.0)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )

        event = InboundEvent(
            platform="telegram", user_id="alice", channel_id="alice", text="hello",
        )
        asyncio.run(router.dispatch(event))
        assert store.session_id_for("alice") is not None

    def test_same_user_messages_reuse_session(self, tmp_path: Path) -> None:
        """End-to-end outclass: 2 dispatches for same user → same session_id."""
        t = [0.0]
        store = TelegramSessionStore(db=tmp_path / "tg.db", clock=lambda: t[0])
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )

        ev1 = InboundEvent(
            platform="telegram", user_id="u42", channel_id="u42", text="first",
        )
        ev2 = InboundEvent(
            platform="telegram", user_id="u42", channel_id="u42", text="second",
        )

        t[0] = 100.0
        asyncio.run(router.dispatch(ev1))
        sid_after_first = store.session_id_for("u42")

        t[0] = 100.0 + (20 * 3600)   # 20h later, within TTL
        asyncio.run(router.dispatch(ev2))
        sid_after_second = store.session_id_for("u42")

        assert sid_after_first == sid_after_second

    def test_on_response_hook_fires(self, tmp_path: Path) -> None:
        calls: list[tuple[str, str]] = []

        async def hook(event: InboundEvent, response: OutboundResponse) -> None:
            calls.append((event.text, response.text))

        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=hook)
        event = InboundEvent(platform="telegram", user_id="u", channel_id="c", text="hi")
        asyncio.run(router.dispatch(event))
        assert len(calls) == 1
        assert calls[0][0] == "hi"

    def test_on_response_failure_does_not_block(self, tmp_path: Path) -> None:
        async def hook(event, response):
            raise RuntimeError("send failed")

        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=hook)
        event = InboundEvent(platform="telegram", user_id="u", channel_id="c", text="hi")
        # Should not raise — exception is logged and swallowed
        resp = asyncio.run(router.dispatch(event))
        assert resp.ok

    def test_sender_wired_as_on_response(self, tmp_path: Path) -> None:
        """Telegram outbound: dispatch → sender.reply_hook → POST sendMessage."""
        calls: list[dict] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            calls.append({"url": url, "body": json.loads(data.decode("utf-8"))})
            return 200, {"ok": True, "result": {"message_id": 1}}

        sender = TelegramSender(bot_token="t", _poster=poster)
        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=sender.reply_hook)
        event = InboundEvent(
            platform="telegram", user_id="42", channel_id="42",
            text="hello", metadata={"message_id": 99},
        )
        asyncio.run(router.dispatch(event))
        assert len(calls) == 1
        assert calls[0]["body"]["chat_id"] == "42"
        assert calls[0]["body"]["reply_to_message_id"] == 99


# ---------------------------------------------------------------------------
# End-to-end verification: message → reply, 24h preserved
# ---------------------------------------------------------------------------

class TestE2EVerification:
    def test_message_reply_24h_preserved(self, tmp_path: Path) -> None:
        """P-52 verification: message → reply, 24h gap preserves session."""
        t = [0.0]
        store = TelegramSessionStore(db=tmp_path / "tg.db", clock=lambda: t[0])
        sent: list[str] = []

        def poster(url, data, headers):
            body = json.loads(data.decode("utf-8"))
            sent.append(body["text"])
            return 200, {"ok": True, "result": {"message_id": len(sent)}}

        sender = TelegramSender(bot_token="t", _poster=poster)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
            on_response=sender.reply_hook,
        )

        # T=0: user sends first message
        t[0] = 0.0
        ev1 = parse_telegram(_tg_update(user_id=42, chat_id=42, text="first msg", message_id=1))
        asyncio.run(router.dispatch(ev1))
        sid_1 = store.session_id_for("42")

        # T+23h: user sends second message, within 24h window
        t[0] = 23 * 3600
        ev2 = parse_telegram(_tg_update(user_id=42, chat_id=42, text="second msg", message_id=2))
        asyncio.run(router.dispatch(ev2))
        sid_2 = store.session_id_for("42")

        # T+50h: silent past 24h window, session resets
        t[0] = 50 * 3600
        ev3 = parse_telegram(_tg_update(user_id=42, chat_id=42, text="much later", message_id=3))
        asyncio.run(router.dispatch(ev3))
        sid_3 = store.session_id_for("42")

        # 3 replies sent (each dispatch invokes sender)
        assert len(sent) == 3
        # 23h gap preserves
        assert sid_1 == sid_2
        # 50h gap resets
        assert sid_3 != sid_2
