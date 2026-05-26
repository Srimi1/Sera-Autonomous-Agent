"""Tests for sera.gateway.platforms.imessage.

P-58 verification: local chat.db poll → agent dispatch → osascript send,
with 24h session continuity.

Outclass claims verified:
- ROWID cursor: no duplicate delivery across polls
- Tapback filter: associated_message_type != 0 never reaches the agent
- Epoch auto-detect: nanosecond and second values both convert correctly
- Zero relay: all logic runs on injected fixtures, no network calls
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import AsyncIterator


from sera.gateway.platforms.imessage import (
    CHAT_DB_SCHEMA,
    DEFAULT_SESSION_TTL_S,
    _build_send_script,
    _escape_applescript,
    cocoa_to_unix,
    iMessagePoller,
    iMessageReader,
    iMessageSender,
    iMessageSessionStore,
)
from sera.gateway.router import InboundEvent, OutboundResponse, Router
from sera.llm.base import StreamChunk

# ---------------------------------------------------------------------------
# Fixture DB helpers
# ---------------------------------------------------------------------------

_COCOA_EPOCH = 978_307_200


def _create_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(CHAT_DB_SCHEMA)
    return con


def _insert_handle(con: sqlite3.Connection, *, rowid: int, id_: str, service: str = "iMessage") -> None:
    con.execute(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
        (rowid, id_, service),
    )
    con.commit()


def _insert_message(
    con: sqlite3.Connection,
    *,
    rowid: int,
    text: str | None,
    date_ns: int,
    is_from_me: int = 0,
    handle_id: int | None = None,
    service: str = "iMessage",
    associated_message_type: int = 0,
) -> None:
    con.execute(
        "INSERT INTO message "
        "(ROWID, text, date, is_from_me, handle_id, service, associated_message_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rowid, text, date_ns, is_from_me, handle_id, service, associated_message_type),
    )
    con.commit()


def _ns(unix: float) -> int:
    """Convert Unix timestamp to Cocoa nanoseconds (Big Sur+ format)."""
    return int((unix - _COCOA_EPOCH) * 1e9)


def _sec(unix: float) -> int:
    """Convert Unix timestamp to Cocoa seconds (pre-Big Sur format)."""
    return int(unix - _COCOA_EPOCH)


# ---------------------------------------------------------------------------
# cocoa_to_unix
# ---------------------------------------------------------------------------

class TestCocoaToUnix:
    def test_nanosecond_format(self) -> None:
        unix = 1_700_000_000.0
        cocoa_ns = (unix - _COCOA_EPOCH) * 1e9
        assert abs(cocoa_to_unix(cocoa_ns) - unix) < 0.001

    def test_second_format(self) -> None:
        unix = 1_700_000_000.0
        cocoa_s = unix - _COCOA_EPOCH
        assert abs(cocoa_to_unix(cocoa_s) - unix) < 1.0

    def test_threshold_discriminates(self) -> None:
        # Values near the boundary: 1e12 is the threshold
        assert cocoa_to_unix(1e17) > 1e9   # nanoseconds → sane unix time
        assert cocoa_to_unix(1e8) > 1e9    # seconds since 2001 → sane unix time

    def test_big_sur_epoch_roundtrip(self) -> None:
        unix = 1_714_000_000.0  # April 2024
        ns = _ns(unix)
        assert abs(cocoa_to_unix(ns) - unix) < 0.01

    def test_pre_big_sur_roundtrip(self) -> None:
        unix = 1_600_000_000.0  # September 2020
        s = _sec(unix)
        assert abs(cocoa_to_unix(s) - unix) < 1.0


# ---------------------------------------------------------------------------
# _escape_applescript
# ---------------------------------------------------------------------------

class TestEscapeAppleScript:
    def test_plain_text_unchanged(self) -> None:
        assert _escape_applescript("hello world") == "hello world"

    def test_double_quote_escaped(self) -> None:
        assert _escape_applescript('say "hi"') == 'say \\"hi\\"'

    def test_backslash_escaped(self) -> None:
        assert _escape_applescript("a\\b") == "a\\\\b"

    def test_backslash_then_quote(self) -> None:
        result = _escape_applescript('\\"')
        assert result == '\\\\\\"'

    def test_empty_string(self) -> None:
        assert _escape_applescript("") == ""


# ---------------------------------------------------------------------------
# _build_send_script
# ---------------------------------------------------------------------------

class TestBuildSendScript:
    def test_contains_handle_and_text(self) -> None:
        script = _build_send_script("+14155551234", "hello")
        assert "+14155551234" in script
        assert "hello" in script

    def test_uses_imessage_service(self) -> None:
        script = _build_send_script("+1", "x")
        assert "iMessage" in script

    def test_quotes_escaped_in_text(self) -> None:
        script = _build_send_script("+1", 'say "hi"')
        assert '\\"hi\\"' in script

    def test_quotes_escaped_in_handle(self) -> None:
        script = _build_send_script('a"b', "text")
        assert '\\"' in script


# ---------------------------------------------------------------------------
# iMessageReader
# ---------------------------------------------------------------------------

class TestIMessageReader:
    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        reader = iMessageReader(db_path=tmp_path / "nonexistent.db")
        assert reader.poll() == []

    def test_empty_db_returns_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        con.close()
        reader = iMessageReader(db_path=db)
        assert reader.poll() == []

    def test_basic_inbound_message(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+14155551234")
        unix_ts = 1_700_000_000.0
        _insert_message(con, rowid=1, text="hello sera", date_ns=_ns(unix_ts), handle_id=1)
        con.close()

        reader = iMessageReader(db_path=db)
        events = reader.poll()
        assert len(events) == 1
        ev = events[0]
        assert ev.platform == "imessage"
        assert ev.user_id == "+14155551234"
        assert ev.text == "hello sera"
        assert abs(ev.timestamp - unix_ts) < 1.0
        assert ev.metadata["rowid"] == 1

    def test_outclass_tapback_filtered(self, tmp_path: Path) -> None:
        """associated_message_type != 0 must never reach the agent."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="real message", date_ns=_ns(1_700_000_000.0), handle_id=1)
        # Tapbacks: heart (2000), thumbs up (2001), etc.
        _insert_message(con, rowid=2, text="Liked \"real message\"", date_ns=_ns(1_700_000_001.0),
                        handle_id=1, associated_message_type=2000)
        _insert_message(con, rowid=3, text="Emphasized \"real message\"", date_ns=_ns(1_700_000_002.0),
                        handle_id=1, associated_message_type=2002)
        con.close()

        reader = iMessageReader(db_path=db)
        events = reader.poll()
        assert len(events) == 1, "Only the real message should pass the tapback filter"
        assert events[0].text == "real message"

    def test_outbound_messages_filtered(self, tmp_path: Path) -> None:
        """is_from_me = 1 must be excluded."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="inbound", date_ns=_ns(1_700_000_000.0), handle_id=1, is_from_me=0)
        _insert_message(con, rowid=2, text="outbound", date_ns=_ns(1_700_000_001.0), handle_id=1, is_from_me=1)
        con.close()

        reader = iMessageReader(db_path=db)
        events = reader.poll()
        assert len(events) == 1
        assert events[0].text == "inbound"

    def test_null_text_filtered(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text=None, date_ns=_ns(1_700_000_000.0), handle_id=1)
        _insert_message(con, rowid=2, text="visible", date_ns=_ns(1_700_000_001.0), handle_id=1)
        con.close()

        events = iMessageReader(db_path=db).poll()
        assert len(events) == 1
        assert events[0].text == "visible"

    def test_empty_text_filtered(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="", date_ns=_ns(1_700_000_000.0), handle_id=1)
        _insert_message(con, rowid=2, text="ok", date_ns=_ns(1_700_000_001.0), handle_id=1)
        con.close()

        events = iMessageReader(db_path=db).poll()
        assert len(events) == 1

    def test_outclass_rowid_cursor_no_duplicates(self, tmp_path: Path) -> None:
        """ROWID cursor: second poll must not re-deliver messages from first poll."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="first", date_ns=_ns(1_700_000_000.0), handle_id=1)
        con.close()

        reader = iMessageReader(db_path=db)
        first = reader.poll()
        assert len(first) == 1

        second = reader.poll()
        assert len(second) == 0, "ROWID cursor must not re-deliver already-seen messages"

    def test_new_messages_after_first_poll(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="first", date_ns=_ns(1_700_000_000.0), handle_id=1)

        reader = iMessageReader(db_path=db)
        first = reader.poll()
        assert len(first) == 1

        _insert_message(con, rowid=2, text="second", date_ns=_ns(1_700_000_001.0), handle_id=1)
        con.close()

        second = reader.poll()
        assert len(second) == 1
        assert second[0].text == "second"

    def test_rowid_cursor_advances(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=10, text="a", date_ns=_ns(1_700_000_000.0), handle_id=1)
        _insert_message(con, rowid=20, text="b", date_ns=_ns(1_700_000_001.0), handle_id=1)
        con.close()

        reader = iMessageReader(db_path=db)
        assert reader.last_rowid == 0
        reader.poll()
        assert reader.last_rowid == 20

    def test_multiple_senders(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+11111")
        _insert_handle(con, rowid=2, id_="+22222")
        _insert_message(con, rowid=1, text="from alice", date_ns=_ns(1_700_000_000.0), handle_id=1)
        _insert_message(con, rowid=2, text="from bob", date_ns=_ns(1_700_000_001.0), handle_id=2)
        con.close()

        events = iMessageReader(db_path=db).poll()
        assert len(events) == 2
        handles = {ev.user_id for ev in events}
        assert handles == {"+11111", "+22222"}

    def test_pre_big_sur_nanosecond_epoch(self, tmp_path: Path) -> None:
        """Seconds-based Cocoa epoch (pre-Big Sur) must parse correctly."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        unix_ts = 1_600_000_000.0
        cocoa_s = _sec(unix_ts)
        _insert_message(con, rowid=1, text="old mac", date_ns=cocoa_s, handle_id=1)
        con.close()

        events = iMessageReader(db_path=db).poll()
        assert len(events) == 1
        assert abs(events[0].timestamp - unix_ts) < 1.0

    def test_initial_rowid_skips_old_messages(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=5, text="old", date_ns=_ns(1_700_000_000.0), handle_id=1)
        _insert_message(con, rowid=10, text="new", date_ns=_ns(1_700_000_001.0), handle_id=1)
        con.close()

        reader = iMessageReader(db_path=db, last_rowid=5)
        events = reader.poll()
        assert len(events) == 1
        assert events[0].text == "new"


# ---------------------------------------------------------------------------
# iMessageSender
# ---------------------------------------------------------------------------

class TestIMessageSender:
    def _sender(self, *, rc: int = 0, stderr: str = "") -> tuple[iMessageSender, list[str]]:
        scripts: list[str] = []

        def runner(script: str) -> tuple[int, str]:
            scripts.append(script)
            return rc, stderr

        return iMessageSender(_runner=runner), scripts

    def test_send_success(self) -> None:
        sender, scripts = self._sender()
        result = asyncio.run(sender.send("+14155551234", "hello"))
        assert result.ok is True
        assert result.handle == "+14155551234"
        assert result.error is None
        assert len(scripts) == 1

    def test_send_failure(self) -> None:
        sender, scripts = self._sender(rc=1, stderr="No buddy found")
        result = asyncio.run(sender.send("+1", "hi"))
        assert result.ok is False
        assert "No buddy found" in (result.error or "")

    def test_empty_text_skipped(self) -> None:
        sender, scripts = self._sender()
        result = asyncio.run(sender.send("+1", ""))
        assert result.ok is False
        assert len(scripts) == 0

    def test_empty_handle_skipped(self) -> None:
        sender, scripts = self._sender()
        result = asyncio.run(sender.send("", "text"))
        assert result.ok is False
        assert len(scripts) == 0

    def test_sent_log_accumulated(self) -> None:
        sender, _ = self._sender()
        asyncio.run(sender.send("+1", "a"))
        asyncio.run(sender.send("+2", "b"))
        assert len(sender.sent_log) == 2

    def test_text_with_quotes_sent_safely(self) -> None:
        sender, scripts = self._sender()
        asyncio.run(sender.send("+1", 'He said "hello"'))
        assert '\\"hello\\"' in scripts[0]

    def test_reply_hook_fires_send(self) -> None:
        sender, scripts = self._sender()
        event = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="ping")
        response = OutboundResponse(event=event, ok=True, text="pong")
        asyncio.run(sender.reply_hook(event, response))
        assert len(scripts) == 1
        assert "pong" in scripts[0]

    def test_reply_hook_empty_text_no_send(self) -> None:
        sender, scripts = self._sender()
        event = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="ping")
        response = OutboundResponse(event=event, ok=True, text="")
        asyncio.run(sender.reply_hook(event, response))
        assert len(scripts) == 0


# ---------------------------------------------------------------------------
# iMessageSessionStore
# ---------------------------------------------------------------------------

class TestIMessageSessionStore:
    def _store(self, tmp_path: Path, *, clock=None) -> iMessageSessionStore:
        return iMessageSessionStore(
            db=tmp_path / "ims.db",
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

    def test_distinct_handles_get_distinct_sessions(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        a = store.get_or_create("+11111")
        b = store.get_or_create("+22222")
        assert a.id != b.id

    def test_resolver_stable_for_same_event(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
        resolver = store.resolver(workspace="/tmp")
        event = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="hi")
        assert resolver(event).id == resolver(event).id

    def test_session_id_for_within_ttl(self, tmp_path: Path) -> None:
        t = [100.0]
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
        assert store.session_id_for("+nobody") is None

    def test_active_count_within_ttl(self, tmp_path: Path) -> None:
        store = self._store(tmp_path, clock=lambda: 0.0)
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
        store = iMessageSessionStore(db=tmp_path / "ims.db", clock=lambda: 0.0)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        event = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="hey")
        asyncio.run(router.dispatch(event))
        assert store.session_id_for("+1") is not None

    def test_same_handle_reuses_session(self, tmp_path: Path) -> None:
        t = [0.0]
        store = iMessageSessionStore(db=tmp_path / "ims.db", clock=lambda: t[0])
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
        )
        ev = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="x")
        t[0] = 100.0
        asyncio.run(router.dispatch(ev))
        sid1 = store.session_id_for("+1")
        t[0] = 100.0 + (20 * 3600)
        asyncio.run(router.dispatch(ev))
        assert store.session_id_for("+1") == sid1

    def test_on_response_hook_fires(self) -> None:
        calls: list[tuple[str, str]] = []

        async def hook(event: InboundEvent, response: OutboundResponse) -> None:
            calls.append((event.text, response.text))

        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=hook)
        event = InboundEvent(platform="imessage", user_id="+1", channel_id="+1", text="imsg")
        asyncio.run(router.dispatch(event))
        assert len(calls) == 1
        assert calls[0][0] == "imsg"

    def test_sender_wired_as_on_response(self) -> None:
        scripts: list[str] = []

        def runner(script: str) -> tuple[int, str]:
            scripts.append(script)
            return 0, ""

        sender = iMessageSender(_runner=runner)
        router = Router(llm_factory=lambda _p: _StubLLM(), on_response=sender.reply_hook)
        event = InboundEvent(platform="imessage", user_id="+14155551234", channel_id="+14155551234", text="hi")
        asyncio.run(router.dispatch(event))
        assert len(scripts) == 1
        assert "+14155551234" in scripts[0]


# ---------------------------------------------------------------------------
# iMessagePoller
# ---------------------------------------------------------------------------

class TestIMessagePoller:
    def test_poller_dispatches_events(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text="ping", date_ns=_ns(1_700_000_000.0), handle_id=1)
        con.close()

        dispatched: list[str] = []

        class FakeRouter:
            async def dispatch(self, event: InboundEvent) -> None:
                dispatched.append(event.text)

        reader = iMessageReader(db_path=db)
        poller = iMessagePoller(reader=reader, router=FakeRouter(), interval_s=0.01)

        async def run_briefly() -> None:
            task = asyncio.create_task(poller.start())
            await asyncio.sleep(0.05)
            poller.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())
        assert "ping" in dispatched

    def test_poller_stop_halts_loop(self, tmp_path: Path) -> None:
        db = tmp_path / "chat.db"
        _create_db(db).close()

        dispatched: list[str] = []

        class FakeRouter:
            async def dispatch(self, event: InboundEvent) -> None:
                dispatched.append(event.text)

        reader = iMessageReader(db_path=db)
        poller = iMessagePoller(reader=reader, router=FakeRouter(), interval_s=0.01)
        poller.stop()   # stop before start
        assert not poller._running


# ---------------------------------------------------------------------------
# E2E verification: poll → dispatch → osascript send, 24h preserved
# ---------------------------------------------------------------------------

class TestE2EVerification:
    def test_poll_dispatch_send_24h_preserved(self, tmp_path: Path) -> None:
        """P-58 outclass: local poll → agent → osascript send, zero relay, 24h continuity."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+14155551234")

        t = [0.0]
        store = iMessageSessionStore(db=tmp_path / "ims.db", clock=lambda: t[0])
        scripts: list[str] = []

        def runner(script: str) -> tuple[int, str]:
            scripts.append(script)
            return 0, ""

        sender = iMessageSender(_runner=runner)
        router = Router(
            llm_factory=lambda _p: _StubLLM(),
            session_resolver=store.resolver(workspace=str(tmp_path)),
            on_response=sender.reply_hook,
        )

        # T=0: first message
        t[0] = 0.0
        _insert_message(con, rowid=1, text="first message", date_ns=_ns(t[0] + _COCOA_EPOCH + 1), handle_id=1)
        reader = iMessageReader(db_path=db)
        ev1 = reader.poll()
        assert len(ev1) == 1
        asyncio.run(router.dispatch(ev1[0]))
        sid1 = store.session_id_for("+14155551234")

        # T+23h: within window
        t[0] = 23 * 3600
        _insert_message(con, rowid=2, text="follow up", date_ns=_ns(t[0] + _COCOA_EPOCH + 1), handle_id=1)
        ev2 = reader.poll()
        assert len(ev2) == 1
        asyncio.run(router.dispatch(ev2[0]))
        sid2 = store.session_id_for("+14155551234")

        # T+50h: session resets
        t[0] = 50 * 3600
        _insert_message(con, rowid=3, text="much later", date_ns=_ns(t[0] + _COCOA_EPOCH + 1), handle_id=1)
        ev3 = reader.poll()
        assert len(ev3) == 1
        asyncio.run(router.dispatch(ev3[0]))
        sid3 = store.session_id_for("+14155551234")

        con.close()

        # 3 replies sent
        assert len(scripts) == 3
        # Each script targets the right handle
        for script in scripts:
            assert "+14155551234" in script
        # 23h gap preserves session
        assert sid1 == sid2, "23h gap must preserve session"
        # 50h gap resets session
        assert sid3 != sid2, "50h gap must reset session"

    def test_tapback_never_triggers_dispatch(self, tmp_path: Path) -> None:
        """Tapbacks are filtered before reaching the router — zero LLM calls."""
        db = tmp_path / "chat.db"
        con = _create_db(db)
        _insert_handle(con, rowid=1, id_="+1")
        _insert_message(con, rowid=1, text='Liked "hello"', date_ns=_ns(1_700_000_000.0),
                        handle_id=1, associated_message_type=2000)
        con.close()

        dispatched: list[str] = []

        class CountingRouter:
            async def dispatch(self, event: InboundEvent) -> None:
                dispatched.append(event.text)

        reader = iMessageReader(db_path=db)
        events = reader.poll()
        assert events == [], "Tapback must be filtered before dispatch"
        assert dispatched == []
