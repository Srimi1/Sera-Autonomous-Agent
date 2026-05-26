"""Telegram adapter — parser + sender + 24h per-user session continuity.

OUTCLASS: messages from the same user_id within 24 hours reuse the same
Sera Session, so the bot remembers the conversation across silent gaps.
Hermes' TG bridge spawns a fresh context every message; OH/OC don't ship TG.

Wire-up:
    store = TelegramSessionStore()
    sender = TelegramSender(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])
    router = Router(
        llm_factory=...,
        on_response=sender.reply_hook,
        session_resolver=store.resolver(workspace="/tmp"),
    )
    server, queue = build_server(parser=lambda p, b: parse_telegram(b))
    ...
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from sera.config import SERA_HOME
from sera.gateway.router import InboundEvent, OutboundResponse
from sera.memory.session import Session

log = logging.getLogger("sera.gateway.telegram")

TELEGRAM_SESSIONS_DB = SERA_HOME / "telegram_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600   # 24 hours — the P-52 outclass


# ---------------------------------------------------------------------------
# Inbound payload parser
# ---------------------------------------------------------------------------

def parse_telegram(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse a Telegram webhook `update` into an InboundEvent.

    Handles both `message` and `edited_message`. Returns None for updates
    that carry no text (stickers, photos, callback queries, etc.) — those
    can be wired later via metadata-only events.
    """
    msg = payload.get("message") or payload.get("edited_message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    sender = msg.get("from", {}) or {}
    chat = msg.get("chat", {}) or {}
    user_id = str(sender.get("id") or "anonymous")
    chat_id = str(chat.get("id") or user_id)

    return InboundEvent(
        platform="telegram",
        user_id=user_id,
        channel_id=chat_id,
        text=text,
        timestamp=float(msg.get("date", time.time())),
        metadata={
            "message_id": msg.get("message_id"),
            "username": sender.get("username"),
            "chat_type": chat.get("type"),
            "edited": "edited_message" in payload,
            "raw": payload,
        },
    )


# ---------------------------------------------------------------------------
# Outbound sender
# ---------------------------------------------------------------------------

@dataclass
class TelegramSendResult:
    ok: bool
    message_id: int | None
    error: str | None = None
    raw: dict[str, Any] | None = None


class TelegramSender:
    """Calls Bot API sendMessage. Stdlib urllib — no httpx dep added.

    For tests, inject a `_poster` callable that mimics urllib.request.urlopen
    behaviour: takes (url, data, headers) and returns (status_code, json_dict).
    """

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://api.telegram.org",
        _poster: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramSender requires a non-empty bot_token")
        self._token = bot_token
        self._base = base_url.rstrip("/")
        self._poster = _poster
        self._timeout = timeout
        self.sent_log: list[dict[str, Any]] = []  # for introspection in tests

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def _post_real(self, url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = {}
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                pass
            return e.code, body

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> TelegramSendResult:
        """POST sendMessage with json body. Runs synchronous urllib in a thread."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode:
            payload["parse_mode"] = parse_mode

        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        url = self._url("sendMessage")
        poster = self._poster or self._post_real

        try:
            status, body = await asyncio.to_thread(poster, url, data, headers)
        except Exception as exc:  # noqa: BLE001
            return TelegramSendResult(ok=False, message_id=None, error=str(exc))

        self.sent_log.append({"chat_id": chat_id, "text": text, "status": status, "body": body})

        if status >= 200 and status < 300 and body.get("ok") is True:
            result = body.get("result") or {}
            return TelegramSendResult(
                ok=True,
                message_id=result.get("message_id"),
                raw=body,
            )
        return TelegramSendResult(
            ok=False, message_id=None,
            error=str(body.get("description") or f"HTTP {status}"),
            raw=body,
        )

    # Router on_response hook — wired with router.on_response=sender.reply_hook
    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        """Send the router's response text back to the originating chat."""
        if not response.text:
            return
        reply_to = None
        meta = event.metadata or {}
        if isinstance(meta.get("message_id"), int):
            reply_to = meta["message_id"]
        await self.send_message(
            chat_id=event.channel_id,
            text=response.text,
            reply_to_message_id=reply_to,
        )


# ---------------------------------------------------------------------------
# 24h session continuity — the P-52 outclass
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_sessions (
    user_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_sessions_last_seen
    ON telegram_sessions(last_seen);
"""


@dataclass
class _SessionRow:
    user_id: str
    session_id: str
    last_seen: float


class TelegramSessionStore:
    """user_id → session_id, expiring after `ttl_s` (default 24h).

    On lookup:
      - if user_id known AND now - last_seen <= ttl → return existing session_id
      - else → create fresh Session, persist user_id mapping, return it
    """

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or TELEGRAM_SESSIONS_DB
        self._ttl_s = ttl_s
        self._clock = clock
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def _lookup(self, user_id: str) -> _SessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen FROM telegram_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _SessionRow(row["user_id"], row["session_id"], float(row["last_seen"]))

    def _upsert(self, user_id: str, session_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO telegram_sessions (user_id, session_id, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen",
                (user_id, session_id, when),
            )
            con.commit()

    def _touch(self, user_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE telegram_sessions SET last_seen = ? WHERE user_id = ?",
                (when, user_id),
            )
            con.commit()

    def get_or_create(
        self,
        user_id: str,
        *,
        workspace: str = "/tmp",
    ) -> Session:
        """Return an active Session for user_id, creating one if expired/missing."""
        now = self._clock()
        existing = self._lookup(user_id)

        if existing is not None and (now - existing.last_seen) <= self._ttl_s:
            session = Session.load(existing.session_id)
            if session is not None:
                self._touch(user_id, now)
                return session
            # Underlying session disappeared — fall through to create fresh.
            log.warning("telegram: session %s gone, recreating for user %s",
                        existing.session_id, user_id)

        session = Session.create(workspace=workspace)
        self._upsert(user_id, session.id, now)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        """Build a Router session_resolver bound to this store."""
        def _resolve(event: InboundEvent) -> Session:
            return self.get_or_create(event.user_id, workspace=workspace)
        return _resolve

    # Inspection / maintenance

    def evict_expired(self) -> int:
        """Remove rows past TTL. Returns count removed."""
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            cur = con.execute(
                "DELETE FROM telegram_sessions WHERE last_seen < ?", (cutoff,)
            )
            con.commit()
            return cur.rowcount

    def active_count(self) -> int:
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM telegram_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])

    def session_id_for(self, user_id: str) -> str | None:
        """Return the active session_id for user_id, or None if missing/expired."""
        row = self._lookup(user_id)
        if row is None:
            return None
        if (self._clock() - row.last_seen) > self._ttl_s:
            return None
        return row.session_id
