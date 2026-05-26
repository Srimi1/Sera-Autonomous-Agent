"""Tests for sera.integrations.{slack,discord,telegram,gmail,imessage}.

Verification (P-46): 24h backfill ingests ≥100 messages per channel.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from sera.integrations.discord import DiscordScanner
from sera.integrations.gmail import GmailScanner
from sera.integrations.imessage import IMessageScanner, _cocoa_to_unix
from sera.integrations.scanner_base import (
    IngestedMessage,
    backfill,
)
from sera.integrations.slack import SlackScanner
from sera.integrations.telegram import TelegramScanner
from sera.memory.tree import MemoryTree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "tree.db", embedding_dim=8)


def _msg(platform: str, channel: str, i: int, ts: float | None = None) -> dict:
    """Build a synthetic message dict for mock clients."""
    return {
        "id": f"{platform}-{channel}-{i}",
        "ts": str(ts or time.time() - i),
        "text": f"Message #{i} in {channel}",
        "user": f"user_{i % 5}",
    }


# ---------------------------------------------------------------------------
# IngestedMessage formatting
# ---------------------------------------------------------------------------

class TestIngestedMessage:
    def test_chunk_content_contains_fields(self) -> None:
        m = IngestedMessage(
            platform="slack", channel="general", sender="alice",
            text="hello world", timestamp=1700000000.0, message_id="m1",
        )
        content = m.chunk_content()
        assert "slack:general" in content
        assert "alice" in content
        assert "hello world" in content

    def test_source_tag(self) -> None:
        m = IngestedMessage(
            platform="discord", channel="C123", sender="bob",
            text="x", timestamp=0.0, message_id="m1",
        )
        assert m.source_tag() == "discord/C123"

    def test_thread_id_shown(self) -> None:
        m = IngestedMessage(
            platform="slack", channel="general", sender="alice",
            text="reply", timestamp=0.0, message_id="m1", thread_id="t123",
        )
        assert "thread=t123" in m.chunk_content()


# ---------------------------------------------------------------------------
# backfill helper
# ---------------------------------------------------------------------------

class TestBackfillHelper:
    def test_backfill_writes_chunks(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path)

        class _S:
            platform = "test"
            async def fetch(self, *, since: float, max_messages: int = 1000):
                return [IngestedMessage(
                    platform="test", channel="c1", sender=f"u{i}",
                    text=f"msg {i}", timestamp=time.time() - i, message_id=str(i),
                ) for i in range(5)]

        result = asyncio.run(backfill(_S(), tree))
        assert result.ok
        assert result.messages_fetched == 5
        assert result.chunks_written == 5

    def test_backfill_handles_fetch_failure(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path)

        class _BadScanner:
            platform = "test"
            async def fetch(self, *, since: float, max_messages: int = 1000):
                raise RuntimeError("network down")

        result = asyncio.run(backfill(_BadScanner(), tree))
        assert not result.ok
        assert "network down" in result.errors[0]
        assert result.messages_fetched == 0

    def test_backfill_since_window(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path)
        captured: list[float] = []

        class _S:
            platform = "test"
            async def fetch(self, *, since: float, max_messages: int = 1000):
                captured.append(since)
                return []

        asyncio.run(backfill(_S(), tree, hours=24.0))
        # since should be roughly now - 24h
        assert captured[0] < time.time() - 23 * 3600


# ---------------------------------------------------------------------------
# SlackScanner
# ---------------------------------------------------------------------------

class TestSlackScanner:
    def test_fetch_via_mock_client(self) -> None:
        class _MockClient:
            def conversations_history(self, *, channel, oldest, limit):
                return {"messages": [_msg("slack", channel, i) for i in range(120)]}

            def conversations_list(self):
                return {"channels": [{"id": "C1"}, {"id": "C2"}]}

        scanner = SlackScanner(channels=["C1"], _client=_MockClient())
        msgs = asyncio.run(scanner.fetch(since=0.0, max_messages=200))
        assert len(msgs) == 120
        assert all(m.platform == "slack" for m in msgs)
        assert all(m.channel == "C1" for m in msgs)

    def test_24h_backfill_writes_100_plus(self, tmp_path: Path) -> None:
        """P-46 verification: 24h backfill ingests ≥100 messages per channel."""
        class _MockClient:
            def conversations_history(self, *, channel, oldest, limit):
                return {"messages": [_msg("slack", channel, i) for i in range(150)]}

        scanner = SlackScanner(channels=["C1"], _client=_MockClient())
        tree = _tree(tmp_path)
        result = asyncio.run(backfill(scanner, tree, hours=24.0))
        assert result.chunks_written >= 100, f"only wrote {result.chunks_written}"

    def test_no_client_raises(self) -> None:
        scanner = SlackScanner(token="x")
        with pytest.raises(RuntimeError):
            asyncio.run(scanner.fetch(since=0.0))


# ---------------------------------------------------------------------------
# DiscordScanner
# ---------------------------------------------------------------------------

class TestDiscordScanner:
    def test_fetch_via_mock_client(self) -> None:
        class _MockClient:
            def get_messages(self, channel_id, *, limit):
                return [{
                    "id": f"d-{i}", "content": f"msg {i}",
                    "timestamp_unix": time.time() - i,
                    "author": {"username": f"user{i}"},
                } for i in range(110)]

        scanner = DiscordScanner(channels=["D1"], _client=_MockClient())
        msgs = asyncio.run(scanner.fetch(since=0.0, max_messages=200))
        assert len(msgs) == 110

    def test_24h_backfill_writes_100_plus(self, tmp_path: Path) -> None:
        class _MockClient:
            def get_messages(self, channel_id, *, limit):
                return [{
                    "id": f"d-{i}", "content": f"msg {i}",
                    "timestamp_unix": time.time() - i,
                    "author": {"username": f"u{i}"},
                } for i in range(150)]

        scanner = DiscordScanner(channels=["D1"], _client=_MockClient())
        tree = _tree(tmp_path)
        result = asyncio.run(backfill(scanner, tree, hours=24.0))
        assert result.chunks_written >= 100

    def test_old_messages_filtered(self) -> None:
        class _MockClient:
            def get_messages(self, channel_id, *, limit):
                # 50 messages: half old, half recent
                return [{
                    "id": f"d-{i}", "content": f"msg {i}",
                    "timestamp_unix": (time.time() - 1000) if i < 25 else time.time(),
                    "author": "u",
                } for i in range(50)]

        scanner = DiscordScanner(channels=["D1"], _client=_MockClient())
        # since = now - 100s; should drop the 25 old messages
        msgs = asyncio.run(scanner.fetch(since=time.time() - 100, max_messages=100))
        assert len(msgs) == 25


# ---------------------------------------------------------------------------
# TelegramScanner
# ---------------------------------------------------------------------------

class TestTelegramScanner:
    def test_24h_backfill_writes_100_plus(self, tmp_path: Path) -> None:
        class _MockClient:
            def iter_messages(self, chat, *, limit):
                return [{
                    "id": i, "message": f"telegram msg {i}",
                    "date_unix": time.time() - i,
                    "sender_id": f"sender_{i % 3}",
                } for i in range(120)]

        scanner = TelegramScanner(chats=["@channel1"], _client=_MockClient())
        tree = _tree(tmp_path)
        result = asyncio.run(backfill(scanner, tree, hours=24.0))
        assert result.chunks_written >= 100


# ---------------------------------------------------------------------------
# GmailScanner
# ---------------------------------------------------------------------------

class TestGmailScanner:
    def test_24h_backfill_writes_100_plus(self, tmp_path: Path) -> None:
        now_ms = int(time.time() * 1000)
        class _MockClient:
            def list_messages(self, *, q):
                return [{"id": f"g-{i}"} for i in range(110)]

            def get_message(self, msg_id):
                return {
                    "id": msg_id,
                    "internalDate": str(now_ms),
                    "snippet": f"email body for {msg_id}",
                    "payload": {"headers": [
                        {"name": "From", "value": "sender@test.com"},
                        {"name": "Subject", "value": "Test"},
                    ]},
                    "threadId": f"th-{msg_id}",
                }

        scanner = GmailScanner(mailbox="inbox", _client=_MockClient())
        tree = _tree(tmp_path)
        result = asyncio.run(backfill(scanner, tree, hours=24.0))
        assert result.chunks_written >= 100


# ---------------------------------------------------------------------------
# IMessageScanner
# ---------------------------------------------------------------------------

class TestIMessageScanner:
    def test_cocoa_seconds_conversion(self) -> None:
        # 2001-01-01 UTC + 0 cocoa seconds = 978307200 unix
        assert _cocoa_to_unix(0.0) == 978_307_200

    def test_cocoa_nanoseconds_conversion(self) -> None:
        # Modern macOS uses nanoseconds. 1e18 cocoa ns = 1e9 cocoa seconds.
        result = _cocoa_to_unix(1e18)
        expected = 978_307_200 + 1e9
        assert abs(result - expected) < 1.0

    def test_db_missing_returns_empty(self) -> None:
        scanner = IMessageScanner(db_path="/nonexistent/chat.db")
        result = asyncio.run(scanner.fetch(since=0.0))
        assert result == []

    def test_24h_backfill_writes_100_plus(self, tmp_path: Path) -> None:
        """Build a fixture chat.db with 150 messages, verify backfill writes ≥100."""
        db_path = tmp_path / "chat.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                date INTEGER,
                is_from_me INTEGER,
                handle_id INTEGER
            );
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """)
        con.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551234')")
        con.execute("INSERT INTO chat (ROWID, chat_identifier) VALUES (1, '+15551234')")
        # 150 messages with recent timestamps
        now_cocoa_ns = int((time.time() - 978_307_200) * 1e9)
        for i in range(150):
            ts = now_cocoa_ns - i * 100_000_000
            con.execute(
                "INSERT INTO message (text, date, is_from_me, handle_id) VALUES (?, ?, ?, ?)",
                (f"iMessage #{i}", ts, i % 2, 1),
            )
            con.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)",
                (i + 1,),
            )
        con.commit()
        con.close()

        scanner = IMessageScanner(db_path=db_path)
        tree = _tree(tmp_path)
        result = asyncio.run(backfill(scanner, tree, hours=24.0))
        assert result.chunks_written >= 100, f"only wrote {result.chunks_written}"


# ---------------------------------------------------------------------------
# All 5 scanners present (P-46 deliverable check)
# ---------------------------------------------------------------------------

class TestAllScannersPresent:
    def test_imports(self) -> None:
        from sera.integrations import slack, discord, telegram, gmail, imessage  # noqa: F401
        assert slack.SlackScanner.platform == "slack"
        assert discord.DiscordScanner.platform == "discord"
        assert telegram.TelegramScanner.platform == "telegram"
        assert gmail.GmailScanner.platform == "gmail"
        assert imessage.IMessageScanner.platform == "imessage"
