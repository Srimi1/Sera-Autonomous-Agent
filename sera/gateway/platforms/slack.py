"""Slack adapter — slash commands + DMs + channels + interactive block approvals.

OUTCLASS: rivals ship text-only Slack bots. Sera ships Block Kit interactive
approvals in-channel: the agent requests human sign-off ("Deploy to prod?")
with native Approve/Reject buttons. Block-action callbacks route back through
the gateway as InboundEvents(surface="block_action") — the agent sees the
decision without polling. No rival (Hermes / OpenHuman / OpenClaw / Discord
adapter) ships this pattern.

Inbound shapes (all → InboundEvent):
  slash_command  pre-decoded form dict with "command" key    surface="slash"
  event_callback channel_type="im" / "mpim"                 surface="dm"
  event_callback channel_type anything else                  surface="channel"
  block_actions  pre-decoded payload JSON with type key      surface="block_action"

Outbound:
  plain text      → chat.postMessage {channel, text}
  approval block  → chat.postMessage {channel, text, blocks}   ← THE OUTCLASS
  block ack       → POST response_url within 3-second window

Note on form payloads: Slack sends slash commands and block_actions as
application/x-www-form-urlencoded. The gateway server currently handles JSON.
Callers must decode the form body (and for block_actions JSON-decode the
"payload" field) before passing the resulting dict to parse_slack.
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

log = logging.getLogger("sera.gateway.slack")

SLACK_SESSIONS_DB = SERA_HOME / "slack_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600

_IM_SURFACES = {"im", "mpim"}  # Slack direct-message channel types


# ---------------------------------------------------------------------------
# Inbound parser — unifies slash + event + block_action
# ---------------------------------------------------------------------------

def _channel_surface(channel_type: str | None) -> str:
    return "dm" if channel_type in _IM_SURFACES else "channel"


def parse_slack(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse any Slack-origin payload dict into a unified InboundEvent.

    Handles slash commands (pre-form-decoded), Event API callbacks, and
    interactive block_actions (pre-payload-decoded). Returns None for
    URL verification challenges, bot self-messages, and unhandled event types.
    """
    payload_type = payload.get("type")

    # Slack sends a challenge on webhook registration — caller echoes it back
    if payload_type == "url_verification":
        return None

    # ─── Slash command (pre-decoded from application/x-www-form-urlencoded) ─
    if "command" in payload:
        user_id = str(payload.get("user_id") or "anonymous")
        channel_id = str(payload.get("channel_id") or user_id)
        text = str(payload.get("text") or "").strip()
        return InboundEvent(
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            text=text or str(payload.get("command", "/unknown")),
            timestamp=time.time(),
            metadata={
                "surface": "slash",
                "command": payload.get("command"),
                "response_url": payload.get("response_url"),
                "trigger_id": payload.get("trigger_id"),
                "team_id": payload.get("team_id"),
                "username": payload.get("user_name"),
                "raw": payload,
            },
        )

    # ─── Interactive block_actions ─────────────────────────────────────────
    if payload_type == "block_actions":
        user = payload.get("user") or {}
        channel = payload.get("channel") or {}
        user_id = str(user.get("id") or "anonymous")
        channel_id = str(channel.get("id") or user_id)
        actions = payload.get("actions") or []
        if not actions:
            return None
        action = actions[0]
        action_id = str(action.get("action_id") or "")
        value = str(action.get("value") or (action.get("selected_option") or {}).get("value") or "")
        decision = value if value else action_id
        return InboundEvent(
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            text=f"User {decision}: {action_id}",
            timestamp=time.time(),
            metadata={
                "surface": "block_action",
                "action_id": action_id,
                "action_value": value,
                "block_id": str(action.get("block_id") or ""),
                "response_url": payload.get("response_url"),
                "message_ts": (payload.get("message") or {}).get("ts"),
                "username": user.get("username"),
                "raw": payload,
            },
        )

    # ─── Event API (event_callback) ────────────────────────────────────────
    if payload_type == "event_callback":
        event = payload.get("event") or {}
        event_type = event.get("type")
        if event_type not in ("message", "app_mention"):
            return None
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return None
        text = str(event.get("text") or "").strip()
        if not text:
            return None
        user_id = str(event.get("user") or "anonymous")
        channel_id = str(event.get("channel") or user_id)
        channel_type = event.get("channel_type")
        surface = _channel_surface(channel_type)
        return InboundEvent(
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            text=text,
            timestamp=float(event.get("ts") or time.time()),
            metadata={
                "surface": surface,
                "channel_type": channel_type,
                "event_type": event_type,
                "message_ts": event.get("ts"),
                "thread_ts": event.get("thread_ts"),
                "team_id": payload.get("team_id"),
                "username": None,
                "raw": payload,
            },
        )

    return None


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------

def _approval_blocks(text: str, action_id: str) -> list[dict[str, Any]]:
    """Build Block Kit blocks for an approval request.

    A section shows the prompt, an actions block has primary Approve and
    danger Reject buttons. action_id encodes the decision context so
    block_action callbacks self-describe the approval that was decided.
    """
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "block_id": f"approval_{action_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": f"{action_id}_approve",
                    "value": "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "style": "danger",
                    "action_id": f"{action_id}_reject",
                    "value": "reject",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Outbound sender
# ---------------------------------------------------------------------------

@dataclass
class SlackSendResult:
    ok: bool
    ts: str | None = None       # Slack message timestamp (serves as unique ID)
    error: str | None = None
    raw: dict[str, Any] | None = None


class SlackSender:
    """Sends replies to Slack via the Web API.

    The approval-block flow is the P-54 outclass: send_approval_block() posts
    a Block Kit message with Approve/Reject buttons. When the user clicks,
    Slack POSTs block_actions to the gateway; parse_slack() converts it to
    InboundEvent(surface="block_action") so the Router sees the decision
    without any polling or separate webhook plumbing.
    """

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://slack.com/api",
        _poster: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not bot_token:
            raise ValueError("SlackSender requires a non-empty bot_token")
        self._token = bot_token
        self._base = base_url.rstrip("/")
        self._poster = _poster
        self._timeout = timeout
        self.sent_log: list[dict[str, Any]] = []

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

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
        headers = self._auth_headers()
        poster = self._poster or self._post_real
        return await asyncio.to_thread(poster, url, data, headers)

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> SlackSendResult:
        """Post a plain-text (mrkdwn) message to a channel or DM."""
        payload: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        url = f"{self._base}/chat.postMessage"
        try:
            status, body = await self._post(url, payload)
        except Exception as exc:  # noqa: BLE001
            return SlackSendResult(ok=False, error=str(exc))
        self.sent_log.append({"kind": "message", "channel_id": channel_id, "status": status, "body": body})
        if body.get("ok") is True:
            return SlackSendResult(ok=True, ts=body.get("ts"), raw=body)
        return SlackSendResult(
            ok=False,
            error=str(body.get("error") or f"HTTP {status}"),
            raw=body,
        )

    async def send_approval_block(
        self,
        channel_id: str,
        prompt: str,
        action_id: str,
        *,
        thread_ts: str | None = None,
    ) -> SlackSendResult:
        """Post an interactive approval request with Approve/Reject buttons.

        This is the P-54 outclass. When the user clicks, Slack POSTs
        block_actions to the gateway; parse_slack() converts it to an
        InboundEvent(surface="block_action", metadata.action_value="approve"
        or "reject"). The Router delivers that decision to the agent in the
        same session context — no polling, no separate integration.

        Args:
            channel_id: Slack channel or IM channel to post into.
            prompt:     Text shown above the buttons (mrkdwn supported).
            action_id:  Unique context identifier (e.g. "deploy_prod_20260524").
                        Button action_ids become "{action_id}_approve/reject".
            thread_ts:  Optional — post in an existing thread.
        """
        blocks = _approval_blocks(prompt, action_id)
        payload: dict[str, Any] = {
            "channel": channel_id,
            "text": prompt,   # fallback for clients that don't render blocks
            "blocks": blocks,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        url = f"{self._base}/chat.postMessage"
        try:
            status, body = await self._post(url, payload)
        except Exception as exc:  # noqa: BLE001
            return SlackSendResult(ok=False, error=str(exc))
        self.sent_log.append({
            "kind": "approval_block", "channel_id": channel_id,
            "action_id": action_id, "status": status, "body": body,
        })
        if body.get("ok") is True:
            return SlackSendResult(ok=True, ts=body.get("ts"), raw=body)
        return SlackSendResult(
            ok=False,
            error=str(body.get("error") or f"HTTP {status}"),
            raw=body,
        )

    async def ack_block_action(
        self,
        response_url: str,
        text: str,
        *,
        replace_original: bool = True,
    ) -> SlackSendResult:
        """Respond to a block_action via its response_url within the 3-second window.

        Replacing the original message removes the buttons after the decision,
        preventing double-clicks. Uses plain headers (no Bearer token) because
        response_url is pre-authenticated.
        """
        payload: dict[str, Any] = {"text": text, "replace_original": replace_original}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        poster = self._poster or self._post_real
        try:
            status, body = await asyncio.to_thread(poster, response_url, data, headers)
        except Exception as exc:  # noqa: BLE001
            return SlackSendResult(ok=False, error=str(exc))
        self.sent_log.append({"kind": "block_ack", "response_url": response_url, "status": status})
        ok = 200 <= status < 300
        return SlackSendResult(
            ok=ok,
            error=None if ok else f"HTTP {status}",
            raw=body if isinstance(body, dict) else None,
        )

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        """Router on_response hook — routes reply by surface.

        block_action → ack via response_url (replaces buttons with decision text)
        slash / dm / channel → chat.postMessage; threads get thread_ts set
        """
        if not response.text:
            return
        meta = event.metadata or {}
        surface = meta.get("surface")

        if surface == "block_action":
            response_url = meta.get("response_url")
            if response_url:
                await self.ack_block_action(response_url, response.text)
                return
            # No response_url — fall through to channel message

        thread_ts = meta.get("thread_ts") if surface in {"channel", "dm"} else None
        await self.send_message(event.channel_id, response.text, thread_ts=thread_ts)


# ---------------------------------------------------------------------------
# Session store — 24h per-user continuity across all surfaces
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slack_sessions (
    user_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL,
    last_surface TEXT NOT NULL DEFAULT 'channel'
);
CREATE INDEX IF NOT EXISTS idx_ss_last_seen ON slack_sessions(last_seen);
"""


@dataclass
class _SlackSessionRow:
    user_id: str
    session_id: str
    last_seen: float
    last_surface: str


class SlackSessionStore:
    """Unified per-user session across slash + DM + channel + block_action.

    The block_action surface is the key: when a user approves a request via
    buttons, their session is the same as when they issued the original slash
    command — so the agent's decision context is intact.
    """

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or SLACK_SESSIONS_DB
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

    def _lookup(self, user_id: str) -> _SlackSessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen, last_surface "
                "FROM slack_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _SlackSessionRow(
            row["user_id"], row["session_id"],
            float(row["last_seen"]), row["last_surface"],
        )

    def _upsert(self, user_id: str, session_id: str, when: float, surface: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO slack_sessions (user_id, session_id, last_seen, last_surface) "
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
                "UPDATE slack_sessions SET last_seen = ?, last_surface = ? WHERE user_id = ?",
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
            log.warning("slack: session %s gone, recreating for user %s",
                        existing.session_id, user_id)
        session = Session.create(workspace=workspace)
        self._upsert(user_id, session.id, now, surface)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        """Router-compatible session_resolver unified across all surfaces."""
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
                "SELECT COUNT(*) FROM slack_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
