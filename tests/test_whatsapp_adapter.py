"""Tests for sera.gateway.platforms.whatsapp — local bridge, privacy-first.

P-55 verification: parse_whatsapp handles DM + group payloads; WhatsAppSender
routes to local bridge; session continuity works across DM + group for same JID.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera.gateway.platforms.whatsapp import (
    WhatsAppSender,
    WhatsAppSessionStore,
    _jid_phone,
    _surface_for,
    parse_whatsapp,
)
from sera.gateway.router import OutboundResponse


# ---------------------------------------------------------------------------
# Payload fixtures
# ---------------------------------------------------------------------------

def _dm(
    *,
    from_jid: str = "14155551234@c.us",
    body: str = "hello Sera",
    timestamp: int = 1700000000,
    message_id: str = "3EB0AAAA",
    sender_name: str = "Alice",
) -> dict[str, Any]:
    return {
        "from": from_jid,
        "chatId": from_jid,
        "body": body,
        "timestamp": timestamp,
        "messageId": message_id,
        "isGroup": False,
        "senderName": sender_name,
    }


def _group(
    *,
    from_jid: str = "14155551234@c.us",
    chat_id: str = "120363000123456789@g.us",
    body: str = "hello group",
    timestamp: int = 1700000001,
    message_id: str = "3EB0BBBB",
    sender_name: str = "Alice",
) -> dict[str, Any]:
    return {
        "from": from_jid,
        "chatId": chat_id,
        "body": body,
        "timestamp": timestamp,
        "messageId": message_id,
        "isGroup": True,
        "senderName": sender_name,
    }


def _broadcast(*, from_jid: str = "status@broadcast", body: str = "status update") -> dict[str, Any]:
    return {
        "from": from_jid,
        "chatId": "status@broadcast",
        "body": body,
        "timestamp": 1700000002,
        "isGroup": False,
    }


# ---------------------------------------------------------------------------
# JID helpers
# ---------------------------------------------------------------------------

def test_jid_phone_strips_c_us():
    assert _jid_phone("14155551234@c.us") == "14155551234"


def test_jid_phone_strips_g_us():
    assert _jid_phone("120363000@g.us") == "120363000"


def test_jid_phone_no_suffix_unchanged():
    assert _jid_phone("14155551234") == "14155551234"


def test_surface_for_group():
    assert _surface_for(True) == "group"
    assert _surface_for(False) == "dm"


# ---------------------------------------------------------------------------
# parse_whatsapp — DM
# ---------------------------------------------------------------------------

def test_parse_dm_basic():
    ev = parse_whatsapp(_dm())
    assert ev is not None
    assert ev.platform == "whatsapp"
    assert ev.user_id == "14155551234@c.us"
    assert ev.channel_id == "14155551234@c.us"
    assert ev.text == "hello Sera"
    assert ev.metadata["surface"] == "dm"
    assert ev.metadata["is_group"] is False
    assert ev.metadata["message_id"] == "3EB0AAAA"
    assert ev.metadata["sender_name"] == "Alice"
    assert ev.metadata["phone"] == "14155551234"


def test_parse_dm_timestamp():
    ev = parse_whatsapp(_dm(timestamp=1700012345))
    assert ev.timestamp == 1700012345.0


def test_parse_dm_no_sender_name():
    payload = _dm()
    del payload["senderName"]
    ev = parse_whatsapp(payload)
    assert ev is not None
    assert ev.metadata["sender_name"] is None


# ---------------------------------------------------------------------------
# parse_whatsapp — group message
# ---------------------------------------------------------------------------

def test_parse_group_basic():
    ev = parse_whatsapp(_group())
    assert ev is not None
    assert ev.user_id == "14155551234@c.us"       # sender JID
    assert ev.channel_id == "120363000123456789@g.us"  # group JID
    assert ev.metadata["surface"] == "group"
    assert ev.metadata["is_group"] is True


def test_parse_group_inferred_from_chat_id():
    payload = _group()
    del payload["isGroup"]
    ev = parse_whatsapp(payload)
    assert ev is not None
    assert ev.metadata["is_group"] is True   # inferred from @g.us suffix


# ---------------------------------------------------------------------------
# parse_whatsapp — filtered / skipped cases
# ---------------------------------------------------------------------------

def test_parse_broadcast_skipped():
    assert parse_whatsapp(_broadcast()) is None


def test_parse_broadcast_chat_id_skipped():
    payload = _dm(from_jid="14155551234@c.us")
    payload["chatId"] = "status@broadcast"
    assert parse_whatsapp(payload) is None


def test_parse_empty_body_skipped():
    assert parse_whatsapp(_dm(body="")) is None


def test_parse_whitespace_body_skipped():
    assert parse_whatsapp(_dm(body="   ")) is None


def test_parse_missing_from_skipped():
    payload = _dm()
    del payload["from"]
    assert parse_whatsapp(payload) is None


def test_parse_empty_payload_skipped():
    assert parse_whatsapp({}) is None


# ---------------------------------------------------------------------------
# WhatsAppSender — loopback enforcement
# ---------------------------------------------------------------------------

def test_sender_rejects_non_loopback():
    with pytest.raises(ValueError, match="loopback"):
        WhatsAppSender(bridge_url="https://api.example.com/send")


def test_sender_rejects_public_ip():
    with pytest.raises(ValueError, match="loopback"):
        WhatsAppSender(bridge_url="http://10.0.0.1:3001")


def test_sender_accepts_loopback_127():
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001",
                            _poster=lambda *_: (200, {}))
    assert "127.0.0.1" in sender._base


def test_sender_accepts_localhost():
    sender = WhatsAppSender(bridge_url="http://localhost:3001",
                            _poster=lambda *_: (200, {}))
    assert "localhost" in sender._base


# ---------------------------------------------------------------------------
# WhatsAppSender — send_message
# ---------------------------------------------------------------------------

def _make_poster(status: int, body: dict[str, Any]):
    def _poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return status, body
    return _poster


def test_send_message_success():
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001",
                            _poster=_make_poster(200, {"ok": True}))
    result = asyncio.run(sender.send_message("14155551234@c.us", "hello"))
    assert result.ok is True
    assert result.error is None


def test_send_message_posts_to_send_endpoint():
    captured = {}
    def _poster(url, data, headers):
        captured["url"] = url
        captured["payload"] = json.loads(data)
        return 200, {}
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001", _poster=_poster)
    asyncio.run(sender.send_message("14155551234@c.us", "hi"))
    assert captured["url"] == "http://127.0.0.1:3001/send"
    assert captured["payload"]["to"] == "14155551234@c.us"
    assert captured["payload"]["body"] == "hi"


def test_send_message_bridge_error():
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001",
                            _poster=_make_poster(500, {"error": "bridge_down"}))
    result = asyncio.run(sender.send_message("14155551234@c.us", "hi"))
    assert result.ok is False
    assert "bridge_down" in result.error


def test_send_message_logs_entry():
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001",
                            _poster=_make_poster(200, {}))
    asyncio.run(sender.send_message("14155551234@c.us", "logged"))
    assert len(sender.sent_log) == 1
    assert sender.sent_log[0]["to"] == "14155551234@c.us"


# ---------------------------------------------------------------------------
# WhatsAppSender — reply_hook
# ---------------------------------------------------------------------------

def test_reply_hook_dm_sends_to_channel_id():
    captured = {}
    def _poster(url, data, headers):
        captured["payload"] = json.loads(data)
        return 200, {}
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001", _poster=_poster)
    event = parse_whatsapp(_dm())
    response = OutboundResponse(event=event, ok=True, text="reply text")
    asyncio.run(sender.reply_hook(event, response))
    assert captured["payload"]["to"] == "14155551234@c.us"
    assert captured["payload"]["body"] == "reply text"


def test_reply_hook_group_sends_to_group_jid():
    captured = {}
    def _poster(url, data, headers):
        captured["payload"] = json.loads(data)
        return 200, {}
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001", _poster=_poster)
    event = parse_whatsapp(_group())
    response = OutboundResponse(event=event, ok=True, text="group reply")
    asyncio.run(sender.reply_hook(event, response))
    assert captured["payload"]["to"] == "120363000123456789@g.us"


def test_reply_hook_empty_text_no_op():
    sender = WhatsAppSender(bridge_url="http://127.0.0.1:3001",
                            _poster=_make_poster(200, {}))
    event = parse_whatsapp(_dm())
    response = OutboundResponse(event=event, ok=True, text="")
    asyncio.run(sender.reply_hook(event, response))
    assert sender.sent_log == []


# ---------------------------------------------------------------------------
# WhatsAppSessionStore
# ---------------------------------------------------------------------------

def test_store_creates_session(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db")
    session = store.get_or_create("14155551234@c.us", surface="dm")
    assert session is not None
    assert store.session_id_for("14155551234@c.us") == session.id


def test_store_same_session_within_ttl(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db", ttl_s=3600)
    s1 = store.get_or_create("14155551234@c.us", surface="dm")
    s2 = store.get_or_create("14155551234@c.us", surface="group")
    assert s1.id == s2.id


def test_store_dm_and_group_same_session(tmp_path):
    """Same sender JID in DM then group → same session (the key design)."""
    store = WhatsAppSessionStore(db=tmp_path / "wa.db")
    ev_dm = parse_whatsapp(_dm())
    ev_group = parse_whatsapp(_group())
    resolver = store.resolver(workspace="/tmp")
    s_dm = resolver(ev_dm)
    s_group = resolver(ev_group)
    assert s_dm.id == s_group.id


def test_store_last_surface_updated(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db")
    store.get_or_create("14155551234@c.us", surface="dm")
    store.get_or_create("14155551234@c.us", surface="group")
    assert store.last_surface_for("14155551234@c.us") == "group"


def test_store_ttl_expiry(tmp_path):
    t = [0.0]
    store = WhatsAppSessionStore(db=tmp_path / "wa.db", ttl_s=100, clock=lambda: t[0])
    s1 = store.get_or_create("14155551234@c.us", surface="dm")
    t[0] = 101.0
    s2 = store.get_or_create("14155551234@c.us", surface="dm")
    assert s1.id != s2.id


def test_store_active_count(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db", ttl_s=3600)
    store.get_or_create("111@c.us")
    store.get_or_create("222@c.us")
    assert store.active_count() == 2


def test_store_unknown_user_returns_none(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db")
    assert store.session_id_for("999@c.us") is None
    assert store.last_surface_for("999@c.us") is None


def test_store_resolver_callable(tmp_path):
    store = WhatsAppSessionStore(db=tmp_path / "wa.db")
    resolver = store.resolver(workspace="/tmp")
    event = parse_whatsapp(_dm())
    session = resolver(event)
    assert session is not None
