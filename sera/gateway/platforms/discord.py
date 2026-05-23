"""Discord adapter — slash + DM + thread unified into one InboundEvent stream.

OUTCLASS: Hermes-style bots fork into per-surface handlers (one for slash,
one for DMs, one for threads). Sera's adapter folds all three into a single
InboundEvent shape with `metadata["surface"]` tagging the origin. The Router
sees one inbox, the session store unifies by user_id so a user can start
in slash, continue in DM, and finish in a thread without losing context.

Inbound surfaces:
  - INTERACTION  (type=2 APPLICATION_COMMAND)  → metadata.surface = "slash"
  - MESSAGE_CREATE channel.type=1               → metadata.surface = "dm"
  - MESSAGE_CREATE channel.type=10/11/12        → metadata.surface = "thread"
  - MESSAGE_CREATE channel.type=0               → metadata.surface = "channel"

Outbound:
  - slash  → POST /interactions/{id}/{token}/callback  (type=4 channel msg)
  - else   → POST /channels/{channel_id}/messages
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

log = logging.getLogger("sera.gateway.discord")

DISCORD_SESSIONS_DB = SERA_HOME / "discord_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600

# Discord channel.type vocabulary (subset)
_CHANNEL_TEXT = 0
_CHANNEL_DM = 1
_CHANNEL_THREADS = {10, 11, 12}  # ANNOUNCEMENT_THREAD, PUBLIC_THREAD, PRIVATE_THREAD


# ---------------------------------------------------------------------------
# Parser — unifies slash + DM + thread + channel
# ---------------------------------------------------------------------------

def _surface_for_channel_type(ct: int | None) -> str:
    if ct == _CHANNEL_DM:
        return "dm"
    if ct in _CHANNEL_THREADS:
        return "thread"
    return "channel"


def _slash_text(data: dict[str, Any]) -> str:
    """Build a human-readable text from a slash command's options.

    Convention: if there's a single string option, return its value.
    Otherwise serialize as `<name>: <value>` lines after the command name.
    """
    name = data.get("name", "")
    options = data.get("options") or []
    if len(options) == 1 and isinstance(options[0].get("value"), str):
        return options[0]["value"]
    if not options:
        return f"/{name}"
    parts = [f"/{name}"]
    for opt in options:
        parts.append(f"{opt.get('name')}: {opt.get('value')}")
    return "\n".join(parts)


def parse_discord(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse any Discord webhook payload into a unified InboundEvent.

    Returns None for payloads we don't handle (PING, MESSAGE_DELETE, bot
    self-messages, etc.).
    """
    # ─── Interaction (slash + components + autocomplete) ─────────────────
    if "type" in payload and payload.get("type") in (2, 3, 4) and "data" in payload:
        itype = payload["type"]
        if itype != 2:                                  # we only handle slash for now
            return None
        data = payload.get("data") or {}
        member = payload.get("member") or {}
        user = (member.get("user") or payload.get("user") or {})
        user_id = str(user.get("id") or "anonymous")
        channel_id = str(payload.get("channel_id") or user_id)
        guild_id = payload.get("guild_id")
        return InboundEvent(
            platform="discord",
            user_id=user_id,
            channel_id=channel_id,
            text=_slash_text(data),
            timestamp=time.time(),
            metadata={
                "surface": "slash",
                "interaction_id": str(payload.get("id", "")),
                "interaction_token": payload.get("token"),
                "command_name": data.get("name"),
                "options": data.get("options"),
                "guild_id": guild_id,
                "username": user.get("username"),
                "raw": payload,
            },
        )

    # ─── Plain message (MESSAGE_CREATE shape) ─────────────────────────────
    # Real Discord gateway sends {"t":"MESSAGE_CREATE","d":{...}}.
    # Bots-on-webhook clients (and our tests) often pass the inner `d` directly.
    msg = payload.get("d") if isinstance(payload.get("d"), dict) else payload

    # Discord uses `content` for the message body
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    msg_type = msg.get("type", 0)
    if msg_type != 0:                                   # only DEFAULT messages
        return None

    author = msg.get("author") or {}
    if author.get("bot") is True:                       # ignore bot messages
        return None

    user_id = str(author.get("id") or "anonymous")
    channel_id = str(msg.get("channel_id") or user_id)
    channel_type = msg.get("channel_type") or msg.get("channel", {}).get("type")
    if channel_type is not None:
        try:
            channel_type = int(channel_type)
        except (TypeError, ValueError):
            channel_type = None
    surface = _surface_for_channel_type(channel_type)

    return InboundEvent(
        platform="discord",
        user_id=user_id,
        channel_id=channel_id,
        text=content,
        timestamp=float(msg.get("timestamp_unix") or time.time()),
        metadata={
            "surface": surface,
            "channel_type": channel_type,
            "message_id": str(msg.get("id", "")) or None,
            "guild_id": msg.get("guild_id"),
            "thread_parent_id": msg.get("parent_id"),
            "username": author.get("username"),
            "raw": payload,
        },
    )


# ---------------------------------------------------------------------------
# Sender — routes by surface
# ---------------------------------------------------------------------------

@dataclass
class DiscordSendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None


class DiscordSender:
    """Sends replies via two endpoints depending on the inbound surface."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://discord.com/api/v10",
        _poster: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token:
            raise ValueError("DiscordSender requires a non-empty bot_token")
        self._token = bot_token
        self._base = base_url.rstrip("/")
        self._poster = _poster
        self._timeout = timeout
        self.sent_log: list[dict[str, Any]] = []

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }

    def _post_real(self, url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body_raw = resp.read().decode("utf-8")
                body = json.loads(body_raw) if body_raw else {}
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
        headers = self._auth_headers()
        poster = self._poster or self._post_real
        return await asyncio.to_thread(poster, url, data, headers)

    # ─── Endpoint 1: channel message ─────────────────────────────────────

    async def send_channel_message(
        self,
        channel_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> DiscordSendResult:
        payload: dict[str, Any] = {"content": content}
        if reply_to_message_id:
            payload["message_reference"] = {"message_id": reply_to_message_id}
        url = f"{self._base}/channels/{channel_id}/messages"
        try:
            status, body = await self._post(url, payload)
        except Exception as exc:  # noqa: BLE001
            return DiscordSendResult(ok=False, error=str(exc))
        self.sent_log.append({"kind": "channel", "channel_id": channel_id, "status": status, "body": body})
        if 200 <= status < 300:
            return DiscordSendResult(ok=True, message_id=str(body.get("id") or ""), raw=body)
        return DiscordSendResult(
            ok=False,
            error=str(body.get("message") or f"HTTP {status}"),
            raw=body,
        )

    # ─── Endpoint 2: interaction callback ────────────────────────────────

    async def respond_interaction(
        self,
        interaction_id: str,
        interaction_token: str,
        content: str,
        *,
        ephemeral: bool = False,
    ) -> DiscordSendResult:
        """Reply to a slash command via the interaction callback endpoint."""
        url = f"{self._base}/interactions/{interaction_id}/{interaction_token}/callback"
        payload: dict[str, Any] = {
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": {"content": content},
        }
        if ephemeral:
            payload["data"]["flags"] = 64
        try:
            status, body = await self._post(url, payload)
        except Exception as exc:  # noqa: BLE001
            return DiscordSendResult(ok=False, error=str(exc))
        self.sent_log.append({"kind": "interaction", "interaction_id": interaction_id, "status": status, "body": body})
        if 200 <= status < 300:
            return DiscordSendResult(ok=True, raw=body)
        return DiscordSendResult(
            ok=False,
            error=str(body.get("message") or f"HTTP {status}"),
            raw=body,
        )

    # ─── Router on_response hook — picks the right endpoint by surface ────

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        if not response.text:
            return
        meta = event.metadata or {}
        surface = meta.get("surface")

        if surface == "slash":
            interaction_id = meta.get("interaction_id")
            interaction_token = meta.get("interaction_token")
            if interaction_id and interaction_token:
                await self.respond_interaction(interaction_id, interaction_token, response.text)
                return
            log.warning("discord: slash event missing interaction_id/token, falling back to channel")

        # DM, thread, channel — all use the channel-message endpoint
        reply_to = meta.get("message_id") if surface in {"thread", "channel"} else None
        await self.send_channel_message(
            event.channel_id, response.text,
            reply_to_message_id=reply_to,
        )


# ---------------------------------------------------------------------------
# Session store — unified by user_id across all surfaces (the outclass)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_sessions (
    user_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    last_seen REAL NOT NULL,
    last_surface TEXT NOT NULL DEFAULT 'channel'
);
CREATE INDEX IF NOT EXISTS idx_ds_last_seen ON discord_sessions(last_seen);
"""


@dataclass
class _DiscordSessionRow:
    user_id: str
    session_id: str
    last_seen: float
    last_surface: str


class DiscordSessionStore:
    """Unified per-user store across slash + DM + thread + channel.

    Outclass: alice's `/ask hello` in a thread, then a DM follow-up, then a
    plain channel reply — all three resolve to the SAME Sera Session. Hermes-
    style bots would treat each surface as a separate context.
    """

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or DISCORD_SESSIONS_DB
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

    def _lookup(self, user_id: str) -> _DiscordSessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen, last_surface "
                "FROM discord_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _DiscordSessionRow(
            row["user_id"], row["session_id"], float(row["last_seen"]),
            row["last_surface"],
        )

    def _upsert(self, user_id: str, session_id: str, when: float, surface: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO discord_sessions (user_id, session_id, last_seen, last_surface) "
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
                "UPDATE discord_sessions SET last_seen = ?, last_surface = ? WHERE user_id = ?",
                (when, surface, user_id),
            )
            con.commit()

    def get_or_create(
        self,
        user_id: str,
        *,
        surface: str = "channel",
        workspace: str = "/tmp",
    ) -> Session:
        now = self._clock()
        existing = self._lookup(user_id)
        if existing is not None and (now - existing.last_seen) <= self._ttl_s:
            session = Session.load(existing.session_id)
            if session is not None:
                self._touch(user_id, now, surface)
                return session
            log.warning("discord: session %s gone, recreating for user %s",
                        existing.session_id, user_id)
        session = Session.create(workspace=workspace)
        self._upsert(user_id, session.id, now, surface)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        """Router-compatible session_resolver, unified across all surfaces."""
        def _resolve(event: InboundEvent) -> Session:
            surface = (event.metadata or {}).get("surface", "channel")
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
                "SELECT COUNT(*) FROM discord_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
