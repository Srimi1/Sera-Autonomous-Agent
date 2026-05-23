"""WhatsApp adapter — local desktop bridge, not Cloud API.

OUTCLASS: Cloud-API bots (Meta Business API, Twilio) route every message
through third-party servers, require business account approval, and expose
message metadata to vendor infrastructure. Sera connects to a local WhatsApp
Web bridge (wweb.js / Baileys) running on the user's machine — messages
never leave the device except via WhatsApp's own E2E-encrypted WebSocket.
Privacy-first. No API key, no vendor approval, no server-side logging.

Architecture:
  ┌──────────────────────────────────────────────────────┐
  │  WhatsApp / WhatsApp Web (E2E encrypted, via Meta)   │
  │            ↕                                         │
  │  Local bridge process (wweb.js or Baileys)           │
  │   inbound: POST http://127.0.0.1:$SERA_PORT/webhook/ │
  │   outbound: POST http://127.0.0.1:$BRIDGE_PORT/send  │
  │            ↕  (loopback only — never leaves machine) │
  │  Sera gateway (this adapter)                         │
  └──────────────────────────────────────────────────────┘

Bridge inbound payload (bridge POSTs JSON to Sera's /webhook/whatsapp):
  {
    "from":        "1234567890@c.us",    WA JID of sender
    "chatId":      "1234567890@c.us",    DM = same as from; group = "...@g.us"
    "body":        "hello",              message text
    "timestamp":   1700000000,           unix epoch (int)
    "messageId":   "3EB0ABCDEF",         WA message ID (dedup key)
    "isGroup":     false,
    "senderName":  "Alice"               display name (optional)
  }

Bridge outbound endpoint (Sera POSTs to bridge):
  POST http://127.0.0.1:<bridge_port>/send
  Content-Type: application/json
  {"to": "<jid>", "body": "<text>"}

Default bridge port: 3001. Override via SERA_WA_BRIDGE_PORT env var.

Note: P-70 adds screen/accessibility hooks for richer OS-level WA interaction.
This phase ships the text gateway; P-70 media/screen support layers on top.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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

log = logging.getLogger("sera.gateway.whatsapp")

WHATSAPP_SESSIONS_DB = SERA_HOME / "whatsapp_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600
DEFAULT_BRIDGE_PORT: int = 3001

# WhatsApp JID suffixes
_JID_USER = "@c.us"
_JID_GROUP = "@g.us"
_JID_BROADCAST = "@broadcast"


# ---------------------------------------------------------------------------
# JID helpers
# ---------------------------------------------------------------------------

def _jid_phone(jid: str) -> str:
    """Strip WA JID suffix → bare phone number for display / logging."""
    for suffix in (_JID_USER, _JID_GROUP, _JID_BROADCAST):
        if jid.endswith(suffix):
            return jid[: -len(suffix)]
    return jid


def _surface_for(is_group: bool) -> str:
    return "group" if is_group else "dm"


# ---------------------------------------------------------------------------
# Inbound parser
# ---------------------------------------------------------------------------

def parse_whatsapp(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse a bridge inbound payload into a unified InboundEvent.

    Returns None for:
    - Status broadcasts (chatId ends with @broadcast)
    - Empty or non-text body
    - Bridge health-check / ping payloads (no "from" key)
    - Self-messages (from == to, i.e. bot echoes)
    """
    sender_jid = payload.get("from") or ""
    if not sender_jid:
        return None

    # Skip status broadcasts
    chat_id = str(payload.get("chatId") or sender_jid)
    if chat_id.endswith(_JID_BROADCAST) or sender_jid.endswith(_JID_BROADCAST):
        return None

    body = str(payload.get("body") or "").strip()
    if not body:
        return None

    is_group: bool = bool(payload.get("isGroup", chat_id.endswith(_JID_GROUP)))
    surface = _surface_for(is_group)

    # user_id is always the sender JID; channel_id is the chat (= sender for DMs)
    return InboundEvent(
        platform="whatsapp",
        user_id=sender_jid,
        channel_id=chat_id,
        text=body,
        timestamp=float(payload.get("timestamp") or time.time()),
        metadata={
            "surface": surface,
            "is_group": is_group,
            "message_id": payload.get("messageId"),
            "sender_name": payload.get("senderName"),
            "phone": _jid_phone(sender_jid),
            "raw": payload,
        },
    )


# ---------------------------------------------------------------------------
# Outbound sender — local bridge HTTP client
# ---------------------------------------------------------------------------

@dataclass
class WhatsAppSendResult:
    ok: bool
    error: str | None = None
    raw: dict[str, Any] | None = None


class WhatsAppSender:
    """Sends messages via the local WhatsApp bridge process.

    Privacy guarantee: bridge_url must be a loopback address. If a non-loopback
    URL is supplied, the sender refuses to initialise — messages must not route
    through external servers.
    """

    _LOOPBACK_PREFIXES = ("http://127.", "http://localhost", "http://[::1]")

    def __init__(
        self,
        *,
        bridge_port: int | None = None,
        bridge_url: str | None = None,
        _poster: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        port = bridge_port or int(os.environ.get("SERA_WA_BRIDGE_PORT", DEFAULT_BRIDGE_PORT))
        url = bridge_url or f"http://127.0.0.1:{port}"
        if not any(url.startswith(p) for p in self._LOOPBACK_PREFIXES):
            raise ValueError(
                f"WhatsAppSender bridge_url must be a loopback address, got {url!r}. "
                "Routing WA messages through external servers violates the privacy guarantee."
            )
        self._base = url.rstrip("/")
        self._poster = _poster
        self._timeout = timeout
        self.sent_log: list[dict[str, Any]] = []

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

    async def _post(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        poster = self._poster or self._post_real
        return await asyncio.to_thread(poster, url, data, headers)

    async def send_message(self, to_jid: str, text: str) -> WhatsAppSendResult:
        """POST a text message to the local bridge for delivery."""
        url = f"{self._base}/send"
        try:
            status, body = await self._post(url, {"to": to_jid, "body": text})
        except Exception as exc:  # noqa: BLE001
            return WhatsAppSendResult(ok=False, error=str(exc))
        self.sent_log.append({"to": to_jid, "text": text, "status": status, "body": body})
        ok = 200 <= status < 300
        return WhatsAppSendResult(
            ok=ok,
            error=None if ok else str(body.get("error") or f"HTTP {status}"),
            raw=body,
        )

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        """Router on_response hook — replies to the originating chat JID."""
        if not response.text:
            return
        await self.send_message(event.channel_id, response.text)


# ---------------------------------------------------------------------------
# Session store — 24h per-sender-JID continuity
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_sessions (
    user_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL,
    last_surface TEXT NOT NULL DEFAULT 'dm'
);
CREATE INDEX IF NOT EXISTS idx_wa_last_seen ON whatsapp_sessions(last_seen);
"""


@dataclass
class _WASessionRow:
    user_id: str
    session_id: str
    last_seen: float
    last_surface: str


class WhatsAppSessionStore:
    """Per-sender-JID session store with 24h continuity.

    user_id is the sender's WA JID (e.g. "14155551234@c.us"). The same
    person in a DM and a group chat gets the same session — Sera remembers
    the conversation regardless of which surface the next message arrives on.
    """

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or WHATSAPP_SESSIONS_DB
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

    def _lookup(self, user_id: str) -> _WASessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen, last_surface "
                "FROM whatsapp_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _WASessionRow(
            row["user_id"], row["session_id"],
            float(row["last_seen"]), row["last_surface"],
        )

    def _upsert(self, user_id: str, session_id: str, when: float, surface: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO whatsapp_sessions (user_id, session_id, last_seen, last_surface) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen, "
                "last_surface = excluded.last_surface",
                (user_id, session_id, when, surface),
            )
            con.commit()

    def _touch(self, user_id: str, when: float, surface: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE whatsapp_sessions SET last_seen = ?, last_surface = ? WHERE user_id = ?",
                (when, surface, user_id),
            )
            con.commit()

    def get_or_create(
        self,
        user_id: str,
        *,
        surface: str = "dm",
        workspace: str = "/tmp",
    ) -> Session:
        now = self._clock()
        existing = self._lookup(user_id)
        if existing is not None and (now - existing.last_seen) <= self._ttl_s:
            session = Session.load(existing.session_id)
            if session is not None:
                self._touch(user_id, now, surface)
                return session
            log.warning("whatsapp: session %s gone, recreating for user %s",
                        existing.session_id, user_id)
        session = Session.create(workspace=workspace)
        self._upsert(user_id, session.id, now, surface)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        """Router-compatible session_resolver, keyed on sender JID."""
        def _resolve(event: InboundEvent) -> Session:
            surface = (event.metadata or {}).get("surface", "dm")
            return self.get_or_create(event.user_id, surface=surface, workspace=workspace)
        return _resolve

    def session_id_for(self, user_id: str) -> str | None:
        row = self._lookup(user_id)
        if row is None or (self._clock() - row.last_seen) > self._ttl_s:
            return None
        return row.session_id

    def last_surface_for(self, user_id: str) -> str | None:
        row = self._lookup(user_id)
        return row.last_surface if row else None

    def active_count(self) -> int:
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM whatsapp_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
