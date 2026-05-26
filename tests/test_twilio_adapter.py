"""Tests for sera.gateway.platforms.twilio — signature validation, segment
accounting, parser, sender, session store, and router integration.

P-57 verification: send + receive an SMS end-to-end via injected poster.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from sera.gateway.platforms.twilio import (
    DEFAULT_SESSION_TTL_S,
    TwilioSender,
    TwilioSessionStore,
    parse_twilio,
    sms_segments,
    validate_signature,
)
from sera.gateway.router import InboundEvent, OutboundResponse, Router
from sera.llm.base import StreamChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sig(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url
    for key in sorted(params.keys()):
        payload += key + str(params[key])
    mac = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _form(
    *,
    from_num: str = "+14155551234",
    to_num: str = "+14155556789",
    body: str = "hello",
    sid: str = "SMabc123",
    account_sid: str = "ACtest",
    num_media: str = "0",
) -> dict[str, Any]:
    return {
        "From": from_num,
        "To": to_num,
        "Body": body,
        "MessageSid": sid,
        "AccountSid": account_sid,
        "NumMedia": num_media,
    }


# ---------------------------------------------------------------------------
# validate_signature
# ---------------------------------------------------------------------------

class TestValidateSignature:
    _URL = "https://example.com/webhook/sms"
    _TOKEN = "test_auth_token"
    _PARAMS = {"From": "+14155551234", "To": "+14155556789", "Body": "hello"}

    def test_valid_signature_accepted(self) -> None:
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        assert validate_signature(self._TOKEN, sig, self._URL, self._PARAMS) is True

    def test_wrong_token_rejected(self) -> None:
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        assert validate_signature("wrong_token", sig, self._URL, self._PARAMS) is False

    def test_tampered_body_rejected(self) -> None:
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        bad = {**self._PARAMS, "Body": "injected"}
        assert validate_signature(self._TOKEN, sig, self._URL, bad) is False

    def test_wrong_url_rejected(self) -> None:
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        assert validate_signature(self._TOKEN, sig, "https://evil.com/hook", self._PARAMS) is False

    def test_empty_signature_rejected(self) -> None:
        assert validate_signature(self._TOKEN, "", self._URL, self._PARAMS) is False

    def test_empty_token_rejected(self) -> None:
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        assert validate_signature("", sig, self._URL, self._PARAMS) is False

    def test_params_order_independent(self) -> None:
        # Params are sorted internally; order of dict insertion shouldn't matter.
        sig = _make_sig(self._TOKEN, self._URL, self._PARAMS)
        shuffled = {"Body": "hello", "To": "+14155556789", "From": "+14155551234"}
        assert validate_signature(self._TOKEN, sig, self._URL, shuffled) is True

    def test_empty_params_accepted_when_sig_matches(self) -> None:
        empty_params: dict[str, str] = {}
        sig = _make_sig(self._TOKEN, self._URL, empty_params)
        assert validate_signature(self._TOKEN, sig, self._URL, empty_params) is True


# ---------------------------------------------------------------------------
# sms_segments
# ---------------------------------------------------------------------------

class TestSmsSegments:
    def test_empty_string(self) -> None:
        assert sms_segments("") == 0

    def test_single_gsm7_segment(self) -> None:
        # 160 ASCII chars → exactly 1 segment
        assert sms_segments("A" * 160) == 1

    def test_single_gsm7_segment_boundary(self) -> None:
        assert sms_segments("A" * 159) == 1

    def test_two_gsm7_segments(self) -> None:
        # 161 chars → ceil(161/153) = 2
        assert sms_segments("A" * 161) == 2

    def test_three_gsm7_segments(self) -> None:
        # 307 chars → ceil(307/153) = 3 (since 2*153=306 < 307)
        assert sms_segments("A" * 307) == 3

    def test_extension_char_counts_double(self) -> None:
        # € counts as 2 septets in GSM-7 extension table
        # 80 '€' chars = 160 septets → 1 segment
        assert sms_segments("€" * 80) == 1

    def test_extension_char_pushes_to_two_segments(self) -> None:
        # 81 '€' = 162 septets → 2 segments (ceil(162/153)=2)
        assert sms_segments("€" * 81) == 2

    def test_curly_braces_are_extension(self) -> None:
        # '{' and '}' are GSM-7 extension chars
        assert sms_segments("{" * 80) == 1
        assert sms_segments("{" * 81) == 2

    def test_ucs2_short_message(self) -> None:
        # Chinese chars are outside GSM-7 → UCS-2; 70 chars → 1 segment
        assert sms_segments("中" * 70) == 1

    def test_ucs2_two_segments(self) -> None:
        # 71 UCS-2 chars → ceil(71/67) = 2
        assert sms_segments("中" * 71) == 2

    def test_ucs2_three_segments(self) -> None:
        # 135 UCS-2 chars → ceil(135/67) = 3 (since 2*67=134 < 135)
        assert sms_segments("中" * 135) == 3

    def test_mixed_ucs2_trigger(self) -> None:
        # Even one non-GSM7 char forces UCS-2 for the whole message.
        # 70 UCS-2 chars = 1 segment; 71 = 2 segments.
        assert sms_segments("A" * 69 + "中") == 1   # exactly 70 UCS-2 chars → 1
        assert sms_segments("A" * 70 + "中") == 2   # 71 UCS-2 chars → 2


# ---------------------------------------------------------------------------
# parse_twilio
# ---------------------------------------------------------------------------

class TestParseTwilio:
    def test_basic_parse(self) -> None:
        e = parse_twilio(_form())
        assert e is not None
        assert e.platform == "twilio"
        assert e.user_id == "+14155551234"
        assert e.channel_id == "+14155551234"
        assert e.text == "hello"
        assert e.metadata["surface"] == "sms"
        assert e.metadata["message_sid"] == "SMabc123"
        assert e.metadata["to"] == "+14155556789"
        assert e.metadata["num_media"] == 0

    def test_no_from_returns_none(self) -> None:
        f = _form()
        f.pop("From")
        assert parse_twilio(f) is None

    def test_empty_from_returns_none(self) -> None:
        assert parse_twilio({**_form(), "From": ""}) is None

    def test_no_body_returns_none(self) -> None:
        f = _form()
        f.pop("Body")
        assert parse_twilio(f) is None

    def test_empty_body_returns_none(self) -> None:
        assert parse_twilio({**_form(), "Body": ""}) is None

    def test_whitespace_body_returns_none(self) -> None:
        assert parse_twilio({**_form(), "Body": "   "}) is None

    def test_num_media_parsed(self) -> None:
        e = parse_twilio(_form(num_media="2"))
        assert e is not None
        assert e.metadata["num_media"] == 2

    def test_invalid_num_media_defaults_zero(self) -> None:
        e = parse_twilio({**_form(), "NumMedia": "nope"})
        assert e is not None
        assert e.metadata["num_media"] == 0

    def test_missing_optional_fields_are_none(self) -> None:
        minimal = {"From": "+15551234", "Body": "hi"}
        e = parse_twilio(minimal)
        assert e is not None
        assert e.metadata["message_sid"] is None
        assert e.metadata["to"] is None

    def test_timestamp_is_recent(self) -> None:
        before = time.time()
        e = parse_twilio(_form())
        after = time.time()
        assert e is not None
        assert before <= e.timestamp <= after


# ---------------------------------------------------------------------------
# TwilioSender
# ---------------------------------------------------------------------------

class TestTwilioSender:
    def _sender(self, responses: list[tuple[int, dict[str, Any]]]) -> tuple[TwilioSender, list[dict]]:
        calls: list[dict] = []
        idx = [0]

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            body = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            calls.append({"url": url, "body": body, "headers": headers})
            resp = responses[idx[0]]
            idx[0] = min(idx[0] + 1, len(responses) - 1)
            return resp

        sender = TwilioSender(
            account_sid="AC123",
            auth_token="secret",
            from_number="+14155556789",
            _poster=poster,
        )
        return sender, calls

    def test_send_success(self) -> None:
        sender, calls = self._sender([(201, {"sid": "SM999"})])
        result = asyncio.run(sender.send_message("+14155551234", "hello"))
        assert result.ok is True
        assert result.sid == "SM999"
        assert result.segments == 1
        assert len(calls) == 1

    def test_send_records_to_correct_number(self) -> None:
        sender, calls = self._sender([(200, {"sid": "SM1"})])
        asyncio.run(sender.send_message("+1999", "test"))
        assert calls[0]["body"]["To"] == "+1999"
        assert calls[0]["body"]["From"] == "+14155556789"
        assert calls[0]["body"]["Body"] == "test"

    def test_send_uses_basic_auth(self) -> None:
        sender, calls = self._sender([(200, {"sid": "X"})])
        asyncio.run(sender.send_message("+1", "x"))
        auth = calls[0]["headers"]["Authorization"]
        assert auth.startswith("Basic ")
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        assert decoded == "AC123:secret"

    def test_send_http_error_returns_failure(self) -> None:
        sender, _ = self._sender([(429, {"message": "rate limited", "error_code": 429})])
        result = asyncio.run(sender.send_message("+1", "x"))
        assert result.ok is False
        assert result.error is not None

    def test_send_twilio_error_code_returns_failure(self) -> None:
        sender, _ = self._sender([(200, {"error_code": 21211, "message": "Invalid To"})])
        result = asyncio.run(sender.send_message("+bad", "x"))
        assert result.ok is False

    def test_sent_log_accumulated(self) -> None:
        sender, _ = self._sender([(200, {"sid": "S1"}), (200, {"sid": "S2"})])
        asyncio.run(sender.send_message("+1", "a"))
        asyncio.run(sender.send_message("+2", "b"))
        assert len(sender.sent_log) == 2

    def test_segment_count_in_result(self) -> None:
        sender, _ = self._sender([(200, {"sid": "X"})])
        long_text = "A" * 200  # 2 GSM-7 segments
        result = asyncio.run(sender.send_message("+1", long_text))
        assert result.segments == 2

    def test_reply_hook_sends_response_text(self) -> None:
        sender, calls = self._sender([(200, {"sid": "X"})])
        event = InboundEvent(
            platform="twilio", user_id="+14155551234", channel_id="+14155551234", text="hi"
        )
        response = OutboundResponse(event=event, ok=True, text="there")
        asyncio.run(sender.reply_hook(event, response))
        assert len(calls) == 1
        assert calls[0]["body"]["Body"] == "there"

    def test_reply_hook_empty_text_does_not_send(self) -> None:
        sender, calls = self._sender([])
        event = InboundEvent(
            platform="twilio", user_id="+1", channel_id="+1", text="hi"
        )
        response = OutboundResponse(event=event, ok=True, text="")
        asyncio.run(sender.reply_hook(event, response))
        assert len(calls) == 0

    def test_missing_credentials_raises(self) -> None:
        with pytest.raises(ValueError):
            TwilioSender(account_sid="", auth_token="", from_number="+1")

    def test_missing_from_number_raises(self) -> None:
        with pytest.raises(ValueError):
            TwilioSender(account_sid="AC1", auth_token="tok", from_number="")


# ---------------------------------------------------------------------------
# TwilioSessionStore
# ---------------------------------------------------------------------------

class TestTwilioSessionStore:
    def _store(self, tmp_path: Path, *, clock=None) -> TwilioSessionStore:
        return TwilioSessionStore(
            db=tmp_path / "twilio.db",
            ttl_s=DEFAULT_SESSION_TTL_S,
            clock=clock or time.time,
        )

    def test_first_call_creates_session(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        sess = store.get_or_create("+1")
        assert sess.id is not None

    def test_within_ttl_reuses_session(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 1000.0
        s1 = store.get_or_create("+1")
        t[0] = 1000.0 + (23 * 3600)
        s2 = store.get_or_create("+1")
        assert s1.id == s2.id, "23h gap must preserve session"

    def test_past_ttl_creates_new_session(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        t[0] = 1000.0
        s1 = store.get_or_create("+1")
        t[0] = 1000.0 + (25 * 3600)
        s2 = store.get_or_create("+1")
        assert s1.id != s2.id, "25h gap must create new session"

    def test_distinct_users_get_distinct_sessions(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 100.0)
        a = store.get_or_create("+11111")
        b = store.get_or_create("+22222")
        assert a.id != b.id

    def test_resolver_returns_stable_session(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        resolver = store.resolver(workspace="/tmp")
        event = InboundEvent(platform="twilio", user_id="+1", channel_id="+1", text="hi")
        s1 = resolver(event)
        s2 = resolver(event)
        assert s1.id == s2.id

    def test_session_id_for_returns_id_within_ttl(self, tmp_path: Path) -> None:
        t = [1000.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        sess = store.get_or_create("+1")
        assert store.session_id_for("+1") == sess.id

    def test_session_id_for_expired_returns_none(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        store.get_or_create("+1")
        t[0] = 25 * 3600
        assert store.session_id_for("+1") is None

    def test_session_id_for_unknown_returns_none(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        assert store.session_id_for("+unknown") is None

    def test_active_count_counts_within_ttl(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        store.get_or_create("+1")
        store.get_or_create("+2")
        assert store.active_count() == 2

    def test_active_count_excludes_expired(self, tmp_path: Path) -> None:
        t = [0.0]
        store = self._store(tmp_path, clock=lambda: t[0])
        store.get_or_create("+1")
        t[0] = 25 * 3600
        assert store.active_count() == 0


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
    def test_session_resolver_invoked(self, tmp_path: Path) -> None:
        store = TwilioSessionStore(db=tmp_path / "tw.db", clock=lambda: 100.0)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        event = InboundEvent(platform="twilio", user_id="+1", channel_id="+1", text="hi")
        asyncio.run(router.dispatch(event))
        assert store.session_id_for("+1") is not None

    def test_same_user_reuses_session(self, tmp_path: Path) -> None:
        t = [0.0]
        store = TwilioSessionStore(db=tmp_path / "tw.db", clock=lambda: t[0])
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        ev = InboundEvent(platform="twilio", user_id="+1", channel_id="+1", text="x")
        t[0] = 100.0
        asyncio.run(router.dispatch(ev))
        sid1 = store.session_id_for("+1")
        t[0] = 100.0 + (20 * 3600)
        asyncio.run(router.dispatch(ev))
        sid2 = store.session_id_for("+1")
        assert sid1 == sid2

    def test_on_response_hook_fires(self, tmp_path: Path) -> None:
        calls: list[tuple[str, str]] = []

        async def hook(event: InboundEvent, response: OutboundResponse) -> None:
            calls.append((event.text, response.text))

        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=hook)
        event = InboundEvent(platform="twilio", user_id="+1", channel_id="+1", text="sms")
        asyncio.run(router.dispatch(event))
        assert len(calls) == 1
        assert calls[0][0] == "sms"

    def test_sender_wired_as_on_response(self, tmp_path: Path) -> None:
        calls: list[dict] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            body = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            calls.append(body)
            return 200, {"sid": "SM1"}

        sender = TwilioSender(
            account_sid="AC1",
            auth_token="tok",
            from_number="+15550000",
            _poster=poster,
        )
        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=sender.reply_hook)
        event = InboundEvent(platform="twilio", user_id="+1", channel_id="+1", text="hi")
        asyncio.run(router.dispatch(event))
        assert len(calls) == 1
        assert calls[0]["To"] == "+1"


# ---------------------------------------------------------------------------
# E2E verification: parse → dispatch → send, 24h preserved
# ---------------------------------------------------------------------------

class TestE2EVerification:
    def test_sms_receive_and_reply_24h_preserved(self, tmp_path: Path) -> None:
        """P-57 verification: inbound SMS → agent response → outbound SMS, 24h continuity."""
        t = [0.0]
        store = TwilioSessionStore(db=tmp_path / "tw.db", clock=lambda: t[0])
        sent: list[str] = []

        def poster(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            body = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
            sent.append(body.get("Body", ""))
            return 200, {"sid": f"SM{len(sent)}"}

        sender = TwilioSender(
            account_sid="AC1",
            auth_token="tok",
            from_number="+15559999",
            _poster=poster,
        )
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
            on_response=sender.reply_hook,
        )

        # T=0: first SMS
        t[0] = 0.0
        ev1 = parse_twilio(_form(from_num="+14155551234", body="first sms"))
        assert ev1 is not None
        asyncio.run(router.dispatch(ev1))
        sid_1 = store.session_id_for("+14155551234")

        # T+23h: within window, session preserved
        t[0] = 23 * 3600
        ev2 = parse_twilio(_form(from_num="+14155551234", body="follow up"))
        assert ev2 is not None
        asyncio.run(router.dispatch(ev2))
        sid_2 = store.session_id_for("+14155551234")

        # T+50h: past window, session resets
        t[0] = 50 * 3600
        ev3 = parse_twilio(_form(from_num="+14155551234", body="much later"))
        assert ev3 is not None
        asyncio.run(router.dispatch(ev3))
        sid_3 = store.session_id_for("+14155551234")

        assert len(sent) == 3
        assert sid_1 == sid_2, "23h gap must preserve session"
        assert sid_3 != sid_2, "50h gap must reset session"

    def test_signature_validated_before_parse(self) -> None:
        """Real deployment would reject without sig; validate_signature + parse_twilio compose."""
        auth_token = "real_token"
        url = "https://sera.app/webhook/sms"
        form = _form(from_num="+1555", body="legit")
        valid_sig = _make_sig(auth_token, url, form)

        assert validate_signature(auth_token, valid_sig, url, form) is True
        ev = parse_twilio(form)
        assert ev is not None
        assert ev.user_id == "+1555"

        # Tampered body rejected before parse would even be called
        bad_form = {**form, "Body": "injected payload"}
        assert validate_signature(auth_token, valid_sig, url, bad_form) is False
