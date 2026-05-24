"""Twilio SMS adapter — inbound webhook + outbound REST, with signature
validation and segment accounting.

This is a foundation adapter (SMS is table stakes), but Sera does not ship it
naively: inbound webhooks are validated against Twilio's X-Twilio-Signature
HMAC so spoofed messages can't drive the agent, and outbound text is measured
in real SMS segments so the budget system (P-39) can price a reply before
sending it. Both are things a one-line Twilio wrapper skips.

Inbound (Twilio POSTs application/x-www-form-urlencoded; caller pre-decodes
the form into a dict before calling parse_twilio):
  {
    "MessageSid": "SM...",
    "From":       "+14155551234",   sender's phone (E.164)
    "To":         "+14155556789",   our Twilio number
    "Body":       "hello",
    "NumMedia":   "0",
    "AccountSid": "AC...",
  }

Outbound (REST):
  POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json
  Authorization: Basic base64(AccountSid:AuthToken)
  Content-Type: application/x-www-form-urlencoded
  To=<e164>&From=<our_number>&Body=<text>

Wire-up:
    store = TwilioSessionStore()
    sender = TwilioSender(account_sid=..., auth_token=..., from_number="+1...")
    router = Router(on_response=sender.reply_hook,
                    session_resolver=store.resolver(...))
    # In the webhook handler, validate first:
    #   if not validate_signature(auth_token, sig, url, form): reject 403
    #   ev = parse_twilio(form)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from sera.config import SERA_HOME
from sera.gateway.router import InboundEvent, OutboundResponse
from sera.memory.session import Session

log = logging.getLogger("sera.gateway.twilio")

TWILIO_SESSIONS_DB = SERA_HOME / "twilio_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600
TWILIO_API_BASE = "https://api.twilio.com"

# GSM-7 basic character set (the chars that fit 160/segment instead of 70).
_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Chars that take two GSM-7 septets (extension table).
_GSM7_EXT = set("^{}\\[~]|€")


# ---------------------------------------------------------------------------
# Signature validation — Twilio X-Twilio-Signature (anti-spoofing)
# ---------------------------------------------------------------------------

def validate_signature(
    auth_token: str,
    signature: str,
    url: str,
    params: dict[str, str],
) -> bool:
    """Validate a Twilio webhook against X-Twilio-Signature.

    Twilio's scheme: take the full request URL, append each POST param's
    key+value sorted by key, HMAC-SHA1 with the auth token, base64-encode,
    and compare. A mismatch means the request did not originate from Twilio
    (or the URL/params were tampered with) and must be rejected.
    """
    if not signature or not auth_token:
        return False
    payload = url
    for key in sorted(params.keys()):
        payload += key + str(params[key])
    mac = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Segment accounting — what a reply will actually cost
# ---------------------------------------------------------------------------

def sms_segments(text: str) -> int:
    """Number of SMS segments `text` will occupy.

    GSM-7 text: 160 chars in a single segment, 153/segment when concatenated.
    Any char outside GSM-7 forces UCS-2 encoding: 70 single, 67/segment.
    Extension-table chars (e.g. €, {, }) count as two GSM-7 septets.
    """
    if not text:
        return 0
    if all(c in _GSM7_BASIC or c in _GSM7_EXT for c in text):
        length = sum(2 if c in _GSM7_EXT else 1 for c in text)
        if length <= 160:
            return 1
        return math.ceil(length / 153)
    # UCS-2
    length = len(text)
    if length <= 70:
        return 1
    return math.ceil(length / 67)


# ---------------------------------------------------------------------------
# Inbound parser
# ---------------------------------------------------------------------------

def parse_twilio(payload: dict[str, Any]) -> InboundEvent | None:
    """Parse a (form-decoded) Twilio inbound SMS webhook into an InboundEvent.

    Returns None when there's no From number or no message body. The reply
    target is the sender's number, so user_id and channel_id are both From.
    """
    from_number = str(payload.get("From") or "").strip()
    if not from_number:
        return None
    body = str(payload.get("Body") or "").strip()
    if not body:
        return None

    num_media = 0
    try:
        num_media = int(payload.get("NumMedia") or 0)
    except (TypeError, ValueError):
        num_media = 0

    return InboundEvent(
        platform="twilio",
        user_id=from_number,
        channel_id=from_number,
        text=body,
        timestamp=time.time(),
        metadata={
            "surface": "sms",
            "message_sid": payload.get("MessageSid"),
            "to": str(payload.get("To") or "") or None,
            "account_sid": payload.get("AccountSid"),
            "num_media": num_media,
            "raw": payload,
        },
    )


# ---------------------------------------------------------------------------
# Outbound sender — Twilio REST
# ---------------------------------------------------------------------------

@dataclass
class TwilioSendResult:
    ok: bool
    sid: str | None = None
    segments: int = 0
    error: str | None = None
    raw: dict[str, Any] | None = None


class TwilioSender:
    """Sends SMS via Twilio's Messages REST endpoint.

    Inject `_poster` (url, data, headers) -> (status, json) for tests; the
    real path POSTs form-encoded data with HTTP Basic auth.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        base_url: str = TWILIO_API_BASE,
        _poster: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not account_sid or not auth_token:
            raise ValueError("TwilioSender requires account_sid and auth_token")
        if not from_number:
            raise ValueError("TwilioSender requires a from_number")
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._base = base_url.rstrip("/")
        self._poster = _poster
        self._timeout = timeout
        self.sent_log: list[dict[str, Any]] = []

    def _auth_headers(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self._account_sid}:{self._auth_token}".encode("utf-8")
        ).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
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

    async def send_message(self, to_number: str, text: str) -> TwilioSendResult:
        url = f"{self._base}/2010-04-01/Accounts/{self._account_sid}/Messages.json"
        form = urllib.parse.urlencode({
            "To": to_number,
            "From": self._from,
            "Body": text,
        }).encode("utf-8")
        headers = self._auth_headers()
        poster = self._poster or self._post_real
        segments = sms_segments(text)
        try:
            status, body = await asyncio.to_thread(poster, url, form, headers)
        except Exception as exc:  # noqa: BLE001
            return TwilioSendResult(ok=False, segments=segments, error=str(exc))
        self.sent_log.append({"to": to_number, "text": text, "status": status, "body": body})
        if 200 <= status < 300 and not body.get("error_code"):
            return TwilioSendResult(
                ok=True, sid=body.get("sid"), segments=segments, raw=body,
            )
        return TwilioSendResult(
            ok=False,
            segments=segments,
            error=str(body.get("message") or body.get("error_code") or f"HTTP {status}"),
            raw=body,
        )

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        """Router on_response hook — texts the response back to the sender."""
        if not response.text:
            return
        await self.send_message(event.channel_id, response.text)


# ---------------------------------------------------------------------------
# Session store — per-phone-number 24h continuity
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS twilio_sessions (
    user_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_twilio_last_seen ON twilio_sessions(last_seen);
"""


@dataclass
class _TwilioSessionRow:
    user_id: str
    session_id: str
    last_seen: float


class TwilioSessionStore:
    """Per-phone-number session store with 24h continuity."""

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or TWILIO_SESSIONS_DB
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

    def _lookup(self, user_id: str) -> _TwilioSessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen FROM twilio_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _TwilioSessionRow(row["user_id"], row["session_id"], float(row["last_seen"]))

    def _upsert(self, user_id: str, session_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO twilio_sessions (user_id, session_id, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen",
                (user_id, session_id, when),
            )
            con.commit()

    def _touch(self, user_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE twilio_sessions SET last_seen = ? WHERE user_id = ?",
                (when, user_id),
            )
            con.commit()

    def get_or_create(self, user_id: str, *, workspace: str = "/tmp") -> Session:
        now = self._clock()
        existing = self._lookup(user_id)
        if existing is not None and (now - existing.last_seen) <= self._ttl_s:
            session = Session.load(existing.session_id)
            if session is not None:
                self._touch(user_id, now)
                return session
            log.warning("twilio: session %s gone, recreating for %s",
                        existing.session_id, user_id)
        session = Session.create(workspace=workspace)
        self._upsert(user_id, session.id, now)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        def _resolve(event: InboundEvent) -> Session:
            return self.get_or_create(event.user_id, workspace=workspace)
        return _resolve

    def session_id_for(self, user_id: str) -> str | None:
        row = self._lookup(user_id)
        if row is None or (self._clock() - row.last_seen) > self._ttl_s:
            return None
        return row.session_id

    def active_count(self) -> int:
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM twilio_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
