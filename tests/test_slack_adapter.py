"""Tests for sera.gateway.platforms.slack — slash + DM + channel + block approvals.

P-54 verification: approval block surfaces from a workspace — block_action
callback routes back as InboundEvent with surface="block_action" in the same
session as the originating slash command.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


from sera.gateway.platforms.slack import (
    SlackSender,
    SlackSessionStore,
    _approval_blocks,
    parse_slack,
)
from sera.gateway.router import OutboundResponse


# ---------------------------------------------------------------------------
# Payload fixtures
# ---------------------------------------------------------------------------

def _slash(
    *,
    user_id: str = "U001",
    channel_id: str = "C001",
    command: str = "/ask",
    text: str = "hello",
    response_url: str = "https://hooks.slack.com/commands/abc",
    team_id: str = "T001",
) -> dict[str, Any]:
    return {
        "command": command,
        "user_id": user_id,
        "user_name": "alice",
        "channel_id": channel_id,
        "text": text,
        "response_url": response_url,
        "trigger_id": "trigger_123",
        "team_id": team_id,
    }


def _channel_event(
    *,
    user: str = "U001",
    channel: str = "C001",
    text: str = "hello",
    channel_type: str = "channel",
    ts: str = "1234567890.123",
    thread_ts: str | None = None,
) -> dict[str, Any]:
    evt: dict[str, Any] = {
        "type": "message",
        "user": user,
        "channel": channel,
        "channel_type": channel_type,
        "text": text,
        "ts": ts,
    }
    if thread_ts:
        evt["thread_ts"] = thread_ts
    return {
        "type": "event_callback",
        "team_id": "T001",
        "event": evt,
    }


def _dm_event(
    *,
    user: str = "U001",
    channel: str = "DM001",
    text: str = "hello dm",
    ts: str = "1234567891.000",
) -> dict[str, Any]:
    return _channel_event(user=user, channel=channel, text=text,
                          channel_type="im", ts=ts)


def _block_action(
    *,
    user_id: str = "U001",
    channel_id: str = "C001",
    action_id: str = "deploy_prod_approve",
    value: str = "approve",
    block_id: str = "approval_deploy_prod",
    response_url: str = "https://hooks.slack.com/actions/xyz",
    message_ts: str = "1234567890.555",
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": user_id, "username": "alice"},
        "channel": {"id": channel_id},
        "actions": [
            {
                "action_id": action_id,
                "block_id": block_id,
                "value": value,
                "type": "button",
            }
        ],
        "response_url": response_url,
        "message": {"ts": message_ts},
    }


def _app_mention(
    *,
    user: str = "U001",
    channel: str = "C001",
    text: str = "<@BOTID> help",
    ts: str = "1234567892.000",
) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T001",
        "event": {
            "type": "app_mention",
            "user": user,
            "channel": channel,
            "channel_type": "channel",
            "text": text,
            "ts": ts,
        },
    }


# ---------------------------------------------------------------------------
# parse_slack — slash command
# ---------------------------------------------------------------------------

def test_parse_slash_basic():
    ev = parse_slack(_slash())
    assert ev is not None
    assert ev.platform == "slack"
    assert ev.user_id == "U001"
    assert ev.channel_id == "C001"
    assert ev.text == "hello"
    assert ev.metadata["surface"] == "slash"
    assert ev.metadata["command"] == "/ask"
    assert ev.metadata["response_url"] == "https://hooks.slack.com/commands/abc"


def test_parse_slash_empty_text_falls_back_to_command():
    payload = _slash(text="")
    ev = parse_slack(payload)
    assert ev is not None
    assert ev.text == "/ask"


def test_parse_slash_preserves_trigger_id():
    ev = parse_slack(_slash())
    assert ev.metadata["trigger_id"] == "trigger_123"
    assert ev.metadata["team_id"] == "T001"
    assert ev.metadata["username"] == "alice"


# ---------------------------------------------------------------------------
# parse_slack — event_callback (channel + DM + app_mention)
# ---------------------------------------------------------------------------

def test_parse_channel_message():
    ev = parse_slack(_channel_event())
    assert ev is not None
    assert ev.platform == "slack"
    assert ev.user_id == "U001"
    assert ev.channel_id == "C001"
    assert ev.text == "hello"
    assert ev.metadata["surface"] == "channel"
    assert ev.metadata["channel_type"] == "channel"
    assert ev.metadata["event_type"] == "message"


def test_parse_dm_message():
    ev = parse_slack(_dm_event())
    assert ev is not None
    assert ev.metadata["surface"] == "dm"
    assert ev.channel_id == "DM001"


def test_parse_mpim_is_dm():
    payload = _channel_event(channel_type="mpim")
    ev = parse_slack(payload)
    assert ev is not None
    assert ev.metadata["surface"] == "dm"


def test_parse_app_mention():
    ev = parse_slack(_app_mention())
    assert ev is not None
    assert ev.metadata["surface"] == "channel"
    assert ev.metadata["event_type"] == "app_mention"
    assert "<@BOTID> help" in ev.text


def test_parse_thread_ts_preserved():
    ev = parse_slack(_channel_event(thread_ts="1234567880.000"))
    assert ev is not None
    assert ev.metadata["thread_ts"] == "1234567880.000"


def test_parse_bot_message_skipped():
    payload = _channel_event()
    payload["event"]["bot_id"] = "BABC"
    assert parse_slack(payload) is None


def test_parse_bot_subtype_skipped():
    payload = _channel_event()
    payload["event"]["subtype"] = "bot_message"
    assert parse_slack(payload) is None


def test_parse_empty_text_skipped():
    payload = _channel_event(text="")
    assert parse_slack(payload) is None


def test_parse_unsupported_event_type_skipped():
    payload = _channel_event()
    payload["event"]["type"] = "reaction_added"
    assert parse_slack(payload) is None


def test_parse_url_verification_returns_none():
    assert parse_slack({"type": "url_verification", "challenge": "xyz"}) is None


def test_parse_unknown_payload_returns_none():
    assert parse_slack({"type": "some_future_type"}) is None


# ---------------------------------------------------------------------------
# parse_slack — block_actions (the outclass surface)
# ---------------------------------------------------------------------------

def test_parse_block_action_approve():
    ev = parse_slack(_block_action(value="approve", action_id="deploy_prod_approve"))
    assert ev is not None
    assert ev.platform == "slack"
    assert ev.user_id == "U001"
    assert ev.channel_id == "C001"
    assert ev.metadata["surface"] == "block_action"
    assert ev.metadata["action_id"] == "deploy_prod_approve"
    assert ev.metadata["action_value"] == "approve"
    assert ev.metadata["response_url"] == "https://hooks.slack.com/actions/xyz"
    assert ev.metadata["message_ts"] == "1234567890.555"
    assert "approve" in ev.text


def test_parse_block_action_reject():
    ev = parse_slack(_block_action(value="reject", action_id="deploy_prod_reject"))
    assert ev is not None
    assert ev.metadata["action_value"] == "reject"


def test_parse_block_action_block_id_preserved():
    ev = parse_slack(_block_action(block_id="approval_deploy_prod"))
    assert ev.metadata["block_id"] == "approval_deploy_prod"


def test_parse_block_action_no_actions_returns_none():
    payload = _block_action()
    payload["actions"] = []
    assert parse_slack(payload) is None


def test_parse_block_action_username_preserved():
    ev = parse_slack(_block_action())
    assert ev.metadata["username"] == "alice"


# ---------------------------------------------------------------------------
# _approval_blocks structure
# ---------------------------------------------------------------------------

def test_approval_blocks_structure():
    blocks = _approval_blocks("Deploy to prod?", "deploy_prod")
    assert len(blocks) == 2
    section, actions = blocks
    assert section["type"] == "section"
    assert section["text"]["text"] == "Deploy to prod?"
    assert section["text"]["type"] == "mrkdwn"
    assert actions["type"] == "actions"
    assert actions["block_id"] == "approval_deploy_prod"


def test_approval_blocks_button_ids_and_values():
    blocks = _approval_blocks("Sure?", "myaction")
    elements = blocks[1]["elements"]
    assert len(elements) == 2
    approve_btn, reject_btn = elements
    assert approve_btn["action_id"] == "myaction_approve"
    assert approve_btn["value"] == "approve"
    assert approve_btn["style"] == "primary"
    assert reject_btn["action_id"] == "myaction_reject"
    assert reject_btn["value"] == "reject"
    assert reject_btn["style"] == "danger"


# ---------------------------------------------------------------------------
# SlackSender — send_message
# ---------------------------------------------------------------------------

def _make_poster(status: int, body: dict[str, Any]):
    def _poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return status, body
    return _poster


def test_send_message_success():
    sender = SlackSender("xoxb-token", _poster=_make_poster(200, {"ok": True, "ts": "111.222"}))
    result = asyncio.run(sender.send_message("C001", "hello"))
    assert result.ok is True
    assert result.ts == "111.222"


def test_send_message_with_thread_ts():
    captured = {}
    def _poster(url, data, headers):
        captured["payload"] = json.loads(data)
        return 200, {"ok": True, "ts": "222.333"}
    sender = SlackSender("xoxb-token", _poster=_poster)
    asyncio.run(sender.send_message("C001", "reply", thread_ts="111.000"))
    assert captured["payload"]["thread_ts"] == "111.000"


def test_send_message_api_error():
    sender = SlackSender("xoxb-token", _poster=_make_poster(200, {"ok": False, "error": "channel_not_found"}))
    result = asyncio.run(sender.send_message("CBAD", "x"))
    assert result.ok is False
    assert "channel_not_found" in result.error


# ---------------------------------------------------------------------------
# SlackSender — send_approval_block
# ---------------------------------------------------------------------------

def test_send_approval_block_posts_blocks():
    captured = {}
    def _poster(url, data, headers):
        captured["payload"] = json.loads(data)
        return 200, {"ok": True, "ts": "999.000"}
    sender = SlackSender("xoxb-token", _poster=_poster)
    result = asyncio.run(sender.send_approval_block("C001", "Deploy to prod?", "deploy_prod"))
    assert result.ok is True
    assert result.ts == "999.000"
    payload = captured["payload"]
    assert payload["channel"] == "C001"
    assert payload["text"] == "Deploy to prod?"
    assert "blocks" in payload
    assert len(payload["blocks"]) == 2


def test_send_approval_block_logs_kind():
    sender = SlackSender("xoxb-token", _poster=_make_poster(200, {"ok": True, "ts": "1.0"}))
    asyncio.run(sender.send_approval_block("C001", "Sure?", "act123"))
    assert sender.sent_log[0]["kind"] == "approval_block"
    assert sender.sent_log[0]["action_id"] == "act123"


def test_send_approval_block_failure():
    sender = SlackSender("xoxb-token", _poster=_make_poster(200, {"ok": False, "error": "invalid_blocks"}))
    result = asyncio.run(sender.send_approval_block("C001", "?", "act"))
    assert result.ok is False
    assert "invalid_blocks" in result.error


# ---------------------------------------------------------------------------
# SlackSender — ack_block_action
# ---------------------------------------------------------------------------

def test_ack_block_action_success():
    captured = {}
    def _poster(url, data, headers):
        captured["url"] = url
        captured["payload"] = json.loads(data)
        return 200, {}
    sender = SlackSender("xoxb-token", _poster=_poster)
    result = asyncio.run(sender.ack_block_action("https://hooks.slack.com/actions/abc", "Approved!"))
    assert result.ok is True
    assert captured["url"] == "https://hooks.slack.com/actions/abc"
    assert captured["payload"]["text"] == "Approved!"
    assert captured["payload"]["replace_original"] is True


def test_ack_block_action_no_auth_header():
    captured_headers = {}
    def _poster(url, data, headers):
        captured_headers.update(headers)
        return 200, {}
    sender = SlackSender("xoxb-token", _poster=_poster)
    asyncio.run(sender.ack_block_action("https://hooks.slack.com/actions/abc", "ok"))
    assert "Authorization" not in captured_headers   # response_url is pre-authenticated


def test_ack_block_action_http_error():
    sender = SlackSender("xoxb-token", _poster=_make_poster(503, {}))
    result = asyncio.run(sender.ack_block_action("https://hooks.slack.com/actions/abc", "ok"))
    assert result.ok is False
    assert "503" in result.error


# ---------------------------------------------------------------------------
# SlackSender — reply_hook routing
# ---------------------------------------------------------------------------

def test_reply_hook_channel_posts_message():
    posted = []
    def _poster(url, data, headers):
        posted.append(json.loads(data))
        return 200, {"ok": True, "ts": "1.0"}
    sender = SlackSender("xoxb-token", _poster=_poster)
    event = parse_slack(_channel_event(text="hi"))
    response = OutboundResponse(event=event, ok=True, text="pong")
    asyncio.run(sender.reply_hook(event, response))
    assert len(posted) == 1
    assert posted[0]["text"] == "pong"


def test_reply_hook_block_action_acks_via_response_url():
    acked = []
    def _poster(url, data, headers):
        acked.append({"url": url, "body": json.loads(data)})
        return 200, {}
    sender = SlackSender("xoxb-token", _poster=_poster)
    event = parse_slack(_block_action())
    response = OutboundResponse(event=event, ok=True, text="Approved. Deploying now.")
    asyncio.run(sender.reply_hook(event, response))
    assert len(acked) == 1
    assert "hooks.slack.com/actions" in acked[0]["url"]
    assert acked[0]["body"]["text"] == "Approved. Deploying now."
    assert acked[0]["body"]["replace_original"] is True


def test_reply_hook_dm_posts_message():
    posted = []
    def _poster(url, data, headers):
        posted.append(json.loads(data))
        return 200, {"ok": True, "ts": "2.0"}
    sender = SlackSender("xoxb-token", _poster=_poster)
    event = parse_slack(_dm_event())
    response = OutboundResponse(event=event, ok=True, text="dm reply")
    asyncio.run(sender.reply_hook(event, response))
    assert posted[0]["channel"] == "DM001"
    assert posted[0]["text"] == "dm reply"


def test_reply_hook_empty_text_no_op():
    sender = SlackSender("xoxb-token", _poster=_make_poster(200, {"ok": True, "ts": "1.0"}))
    event = parse_slack(_channel_event())
    response = OutboundResponse(event=event, ok=True, text="")
    asyncio.run(sender.reply_hook(event, response))
    assert sender.sent_log == []


# ---------------------------------------------------------------------------
# SlackSessionStore
# ---------------------------------------------------------------------------

def test_store_creates_session(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db")
    session = store.get_or_create("U001", surface="slash")
    assert session is not None
    sid = store.session_id_for("U001")
    assert sid == session.id


def test_store_same_session_within_ttl(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db", ttl_s=3600)
    s1 = store.get_or_create("U001", surface="slash")
    s2 = store.get_or_create("U001", surface="channel")
    assert s1.id == s2.id


def test_store_block_action_same_session_as_slash(tmp_path):
    """The outclass: approval click reuses the same session as the slash command."""
    store = SlackSessionStore(db=tmp_path / "s.db")
    slash_session = store.get_or_create("U001", surface="slash")
    block_session = store.get_or_create("U001", surface="block_action")
    assert slash_session.id == block_session.id


def test_store_last_surface_updated(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db")
    store.get_or_create("U001", surface="slash")
    store.get_or_create("U001", surface="block_action")
    assert store.last_surface_for("U001") == "block_action"


def test_store_ttl_expiry(tmp_path):
    t = [0.0]
    store = SlackSessionStore(db=tmp_path / "s.db", ttl_s=100, clock=lambda: t[0])
    s1 = store.get_or_create("U001", surface="slash")
    t[0] = 101.0
    s2 = store.get_or_create("U001", surface="slash")
    assert s1.id != s2.id


def test_store_active_count(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db", ttl_s=3600)
    store.get_or_create("U001")
    store.get_or_create("U002")
    assert store.active_count() == 2


def test_store_resolver_callable(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db")
    resolver = store.resolver(workspace="/tmp")
    event = parse_slack(_slash())
    session = resolver(event)
    assert session is not None


def test_store_unknown_user_returns_none(tmp_path):
    store = SlackSessionStore(db=tmp_path / "s.db")
    assert store.session_id_for("UNEW") is None
    assert store.last_surface_for("UNEW") is None
