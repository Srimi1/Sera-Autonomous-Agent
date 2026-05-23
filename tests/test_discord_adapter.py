"""Tests for sera.gateway.platforms.discord — slash + DM + thread unified.

P-53 verification: slash command in thread; DM also works. Same user across
all three surfaces resolves to the same Session.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from sera.gateway.platforms.discord import (
    DiscordSendResult,
    DiscordSender,
    DiscordSessionStore,
    parse_discord,
)
from sera.gateway.router import InboundEvent, OutboundResponse, Router
from sera.llm.base import StreamChunk


# ---------------------------------------------------------------------------
# Payload fixtures
# ---------------------------------------------------------------------------

def _dm_msg(*, user_id: int = 7, channel_id: int = 100, content: str = "hi", msg_id: int = 1) -> dict:
    return {
        "id": str(msg_id),
        "channel_id": str(channel_id),
        "channel_type": 1,           # DM
        "type": 0,                   # DEFAULT
        "author": {"id": str(user_id), "username": "alice", "bot": False},
        "content": content,
    }


def _channel_msg(*, user_id: int = 7, channel_id: int = 200, guild_id: int = 999,
                 content: str = "hi", msg_id: int = 2) -> dict:
    return {
        "id": str(msg_id),
        "channel_id": str(channel_id),
        "channel_type": 0,
        "type": 0,
        "guild_id": str(guild_id),
        "author": {"id": str(user_id), "username": "alice", "bot": False},
        "content": content,
    }


def _thread_msg(*, user_id: int = 7, thread_id: int = 333, parent_id: int = 200,
                content: str = "hi", msg_id: int = 3) -> dict:
    return {
        "id": str(msg_id),
        "channel_id": str(thread_id),
        "channel_type": 11,          # PUBLIC_THREAD
        "type": 0,
        "parent_id": str(parent_id),
        "author": {"id": str(user_id), "username": "alice", "bot": False},
        "content": content,
    }


def _slash(*, user_id: int = 7, channel_id: int = 200, guild_id: int = 999,
           command: str = "ask", value: str = "hello", interaction_id: int = 555,
           token: str = "tok123") -> dict:
    return {
        "id": str(interaction_id),
        "type": 2,                   # APPLICATION_COMMAND
        "token": token,
        "channel_id": str(channel_id),
        "guild_id": str(guild_id),
        "member": {"user": {"id": str(user_id), "username": "alice"}},
        "data": {
            "name": command,
            "options": [{"name": "q", "value": value, "type": 3}],
        },
    }


# ---------------------------------------------------------------------------
# parse_discord — all four surfaces
# ---------------------------------------------------------------------------

class TestParseDiscord:
    def test_dm(self) -> None:
        e = parse_discord(_dm_msg(content="hello DM"))
        assert e is not None
        assert e.platform == "discord"
        assert e.user_id == "7"
        assert e.text == "hello DM"
        assert e.metadata["surface"] == "dm"

    def test_channel(self) -> None:
        e = parse_discord(_channel_msg(content="hello channel"))
        assert e is not None
        assert e.metadata["surface"] == "channel"
        assert e.metadata["guild_id"] == "999"

    def test_thread(self) -> None:
        e = parse_discord(_thread_msg(content="hello thread"))
        assert e is not None
        assert e.metadata["surface"] == "thread"
        assert e.metadata["thread_parent_id"] == "200"
        assert e.channel_id == "333"

    def test_slash_with_option(self) -> None:
        e = parse_discord(_slash(value="hello world"))
        assert e is not None
        assert e.metadata["surface"] == "slash"
        assert e.metadata["command_name"] == "ask"
        assert e.metadata["interaction_id"] == "555"
        assert e.metadata["interaction_token"] == "tok123"
        assert e.text == "hello world"

    def test_slash_no_options(self) -> None:
        payload = _slash()
        payload["data"]["options"] = []
        e = parse_discord(payload)
        assert e is not None
        assert e.text == "/ask"

    def test_slash_multiple_options(self) -> None:
        payload = _slash()
        payload["data"]["options"] = [
            {"name": "topic", "value": "agents"},
            {"name": "lang", "value": "python"},
        ]
        e = parse_discord(payload)
        assert e is not None
        assert "topic: agents" in e.text
        assert "lang: python" in e.text

    def test_bot_message_ignored(self) -> None:
        msg = _dm_msg()
        msg["author"]["bot"] = True
        assert parse_discord(msg) is None

    def test_empty_content_ignored(self) -> None:
        assert parse_discord(_dm_msg(content="")) is None

    def test_non_default_message_type_ignored(self) -> None:
        msg = _dm_msg()
        msg["type"] = 7   # CHANNEL_FOLLOW_ADD
        assert parse_discord(msg) is None

    def test_unknown_interaction_type_ignored(self) -> None:
        payload = _slash()
        payload["type"] = 3   # MESSAGE_COMPONENT
        assert parse_discord(payload) is None

    def test_wrapped_in_d_envelope(self) -> None:
        """Real Discord gateway wraps the message under "d"."""
        envelope = {"t": "MESSAGE_CREATE", "d": _channel_msg(content="wrapped")}
        e = parse_discord(envelope)
        assert e is not None
        assert e.text == "wrapped"


# ---------------------------------------------------------------------------
# DiscordSender — routes by surface
# ---------------------------------------------------------------------------

class TestDiscordSender:
    def _capture(self) -> tuple[list[dict], Any]:
        calls: list[dict] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            calls.append({"url": url, "headers": headers, "body": json.loads(data.decode("utf-8"))})
            return 200, {"id": "msg_999", "content": json.loads(data.decode("utf-8")).get("content", "")}

        return calls, poster

    def test_channel_message_endpoint(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        result = asyncio.run(sender.send_channel_message("c1", "hi"))
        assert result.ok
        assert calls[0]["url"].endswith("/channels/c1/messages")
        assert calls[0]["body"] == {"content": "hi"}
        assert calls[0]["headers"]["Authorization"] == "Bot t"

    def test_channel_reply_with_message_reference(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        asyncio.run(sender.send_channel_message("c1", "hi", reply_to_message_id="m99"))
        assert calls[0]["body"]["message_reference"] == {"message_id": "m99"}

    def test_interaction_callback_endpoint(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        result = asyncio.run(sender.respond_interaction("int_id", "tok", "reply"))
        assert result.ok
        assert calls[0]["url"].endswith("/interactions/int_id/tok/callback")
        assert calls[0]["body"]["type"] == 4
        assert calls[0]["body"]["data"]["content"] == "reply"

    def test_interaction_ephemeral_flag(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        asyncio.run(sender.respond_interaction("i", "t", "reply", ephemeral=True))
        assert calls[0]["body"]["data"]["flags"] == 64

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="bot_token"):
            DiscordSender(bot_token="")

    def test_reply_hook_slash_uses_interaction_endpoint(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        event = InboundEvent(
            platform="discord", user_id="7", channel_id="c1", text="hi",
            metadata={"surface": "slash", "interaction_id": "i1", "interaction_token": "tok"},
        )
        response = OutboundResponse(event=event, ok=True, text="answer")
        asyncio.run(sender.reply_hook(event, response))
        assert "/interactions/i1/tok/callback" in calls[0]["url"]

    def test_reply_hook_dm_uses_channel_endpoint(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        event = InboundEvent(
            platform="discord", user_id="7", channel_id="dm_c", text="hi",
            metadata={"surface": "dm"},
        )
        response = OutboundResponse(event=event, ok=True, text="answer")
        asyncio.run(sender.reply_hook(event, response))
        assert "/channels/dm_c/messages" in calls[0]["url"]

    def test_reply_hook_thread_uses_channel_endpoint_with_ref(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        event = InboundEvent(
            platform="discord", user_id="7", channel_id="thread_id", text="hi",
            metadata={"surface": "thread", "message_id": "m_parent"},
        )
        response = OutboundResponse(event=event, ok=True, text="answer")
        asyncio.run(sender.reply_hook(event, response))
        assert "/channels/thread_id/messages" in calls[0]["url"]
        assert calls[0]["body"]["message_reference"] == {"message_id": "m_parent"}

    def test_reply_hook_skips_empty_text(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        event = InboundEvent(platform="discord", user_id="u", channel_id="c", text="hi")
        response = OutboundResponse(event=event, ok=True, text="")
        asyncio.run(sender.reply_hook(event, response))
        assert calls == []

    def test_reply_hook_slash_without_token_falls_back(self) -> None:
        calls, poster = self._capture()
        sender = DiscordSender(bot_token="t", _poster=poster)
        event = InboundEvent(
            platform="discord", user_id="7", channel_id="c1", text="hi",
            metadata={"surface": "slash"},  # interaction_id/token missing
        )
        response = OutboundResponse(event=event, ok=True, text="answer")
        asyncio.run(sender.reply_hook(event, response))
        # Should fall back to channel endpoint
        assert "/channels/c1/messages" in calls[0]["url"]


# ---------------------------------------------------------------------------
# DiscordSessionStore — unified by user_id (the outclass)
# ---------------------------------------------------------------------------

class TestUnifiedSessionStore:
    def _store(self, tmp_path: Path, *, clock=None) -> DiscordSessionStore:
        return DiscordSessionStore(db=tmp_path / "d.db", clock=clock or (lambda: 100.0))

    def test_same_user_across_surfaces_same_session(self, tmp_path: Path) -> None:
        """Outclass: alice/slash, alice/DM, alice/thread → all the same Session."""
        store = self._store(tmp_path, clock=lambda: 100.0)
        s_slash = store.get_or_create("alice", surface="slash")
        s_dm = store.get_or_create("alice", surface="dm")
        s_thread = store.get_or_create("alice", surface="thread")
        assert s_slash.id == s_dm.id == s_thread.id

    def test_last_surface_tracked(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.get_or_create("alice", surface="slash")
        assert store.last_surface_for("alice") == "slash"
        store.get_or_create("alice", surface="dm")
        assert store.last_surface_for("alice") == "dm"

    def test_24h_boundary(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 0.0
        s1 = store.get_or_create("alice", surface="dm")
        t[0] = 23 * 3600
        s2 = store.get_or_create("alice", surface="slash")
        assert s1.id == s2.id
        # last_seen now = 23h. Jump 25h further (48h total) → past TTL since last touch.
        t[0] = 48 * 3600
        s3 = store.get_or_create("alice", surface="thread")
        assert s3.id != s1.id

    def test_distinct_users_distinct_sessions(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        a = store.get_or_create("alice")
        b = store.get_or_create("bob")
        assert a.id != b.id

    def test_resolver_unifies_event_surfaces(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        resolver = store.resolver(workspace="/tmp")
        slash_ev = InboundEvent(
            platform="discord", user_id="alice", channel_id="c", text="x",
            metadata={"surface": "slash"},
        )
        dm_ev = InboundEvent(
            platform="discord", user_id="alice", channel_id="c", text="y",
            metadata={"surface": "dm"},
        )
        assert resolver(slash_ev).id == resolver(dm_ev).id


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------

class _StubLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="reply")
        yield StreamChunk(finish_reason="stop")


class TestRouterIntegration:
    def test_three_surfaces_one_session_e2e(self, tmp_path: Path) -> None:
        """P-53 verification (full chain): slash, DM, thread all hit same session."""
        store = DiscordSessionStore(db=tmp_path / "d.db", clock=lambda: 100.0)
        sent: list[dict] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            sent.append({"url": url, "body": json.loads(data.decode("utf-8"))})
            return 200, {"id": "m1"}

        sender = DiscordSender(bot_token="t", _poster=poster)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
            on_response=sender.reply_hook,
        )

        slash_ev = parse_discord(_slash(user_id=7))
        dm_ev = parse_discord(_dm_msg(user_id=7))
        thread_ev = parse_discord(_thread_msg(user_id=7))
        assert slash_ev and dm_ev and thread_ev

        asyncio.run(router.dispatch(slash_ev))
        asyncio.run(router.dispatch(dm_ev))
        asyncio.run(router.dispatch(thread_ev))

        # Three dispatches, three outbound calls
        assert len(sent) == 3
        # First was interaction callback (slash)
        assert "/interactions/" in sent[0]["url"]
        # Other two were channel-message endpoint
        assert "/channels/" in sent[1]["url"]
        assert "/channels/" in sent[2]["url"]
        # Same session_id for user 7 throughout
        assert store.session_id_for("7") is not None
        assert store.active_count() == 1


# ---------------------------------------------------------------------------
# DiscordSendResult shape
# ---------------------------------------------------------------------------

class TestSendResult:
    def test_ok_default(self) -> None:
        r = DiscordSendResult(ok=True, message_id="m1")
        assert r.ok
        assert r.error is None

    def test_failed_with_error(self) -> None:
        r = DiscordSendResult(ok=False, error="missing perms")
        assert not r.ok
        assert "perms" in r.error
