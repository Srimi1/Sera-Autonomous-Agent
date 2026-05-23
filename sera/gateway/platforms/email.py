"""Email adapter — IMAP receive + SMTP send with real RFC 5322 threading.

OUTCLASS: most agent email integrations fire-and-forget a fresh message per
reply, so responses scatter across the recipient's inbox as disconnected
threads. Sera's replies preserve the subject (Re: prefix, no double-prefix)
and set In-Reply-To + a correctly-extended References chain — so the reply
lands inside the original thread in Gmail, Outlook, Apple Mail, everywhere.
Threading is the product, not an afterthought.

Receive path (poll-based, not webhook):
  EmailPoller.poll_unseen() → IMAP SEARCH UNSEEN → FETCH RFC822 → parse_email()
  → mark \\Seen. Inject _client_factory for tests; real path uses imaplib.

Send path:
  EmailSender.build_reply() constructs an EmailMessage with:
    Subject:     Re: <original> (idempotent — won't stack "Re: Re:")
    In-Reply-To: <original Message-ID>
    References:  <original References> <original Message-ID>   (RFC 5322 §3.6.4)
    Message-ID:  freshly generated
  send_reply() ships it via SMTP (inject _transport for tests).

Wire-up:
    store = EmailSessionStore()
    sender = EmailSender(smtp_host=..., username=..., password=...,
                         from_addr="sera@example.com")
    poller = EmailPoller(imap_host=..., username=..., password=...)
    # In a loop: for ev in poller.poll_unseen(): await router.dispatch(ev)
    # router.on_response = sender.reply_hook
"""
from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import logging
import re
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Generator

from sera.config import SERA_HOME
from sera.gateway.router import InboundEvent, OutboundResponse
from sera.memory.session import Session

log = logging.getLogger("sera.gateway.email")

EMAIL_SESSIONS_DB = SERA_HOME / "email_sessions.db"
DEFAULT_SESSION_TTL_S: int = 7 * 24 * 3600   # email threads live longer than chat

_RE_PREFIX = re.compile(r"(?i)^\s*re\s*:")


# ---------------------------------------------------------------------------
# Header / body helpers
# ---------------------------------------------------------------------------

def re_subject(subject: str | None) -> str:
    """Prefix 'Re: ' unless already present. Idempotent — no 'Re: Re:' stacking."""
    s = (subject or "").strip()
    if not s:
        return "Re: (no subject)"
    if _RE_PREFIX.match(s):
        return s
    return f"Re: {s}"


def build_references(original_references: str | None, original_message_id: str | None) -> str:
    """Build the reply's References header per RFC 5322 §3.6.4.

    The reply's References = the parent's References (if any) with the parent's
    Message-ID appended. This is what threads the conversation in mail clients.
    """
    refs: list[str] = []
    if original_references:
        refs.extend(original_references.split())
    if original_message_id and original_message_id not in refs:
        refs.append(original_message_id)
    return " ".join(refs)


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_body(msg: EmailMessage) -> str:
    """Pull the best human-readable body — prefer text/plain, fall back to stripped HTML."""
    try:
        body_part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        body_part = None
    if body_part is None:
        try:
            content = msg.get_content()
        except Exception:  # noqa: BLE001
            return ""
        return content if isinstance(content, str) else ""
    try:
        content = body_part.get_content()
    except Exception:  # noqa: BLE001
        return ""
    if body_part.get_content_type() == "text/html":
        return _strip_html(content)
    return content


# ---------------------------------------------------------------------------
# Inbound parser
# ---------------------------------------------------------------------------

def parse_email(raw: bytes | str) -> InboundEvent | None:
    """Parse a raw RFC822 message into a unified InboundEvent.

    Returns None for messages with no parseable From address or empty body.
    Threading headers (Message-ID, In-Reply-To, References) are carried in
    metadata so the reply can be threaded correctly.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as exc:  # noqa: BLE001
        log.warning("email: failed to parse message: %s", exc)
        return None

    from_hdr = msg.get("From")
    if not from_hdr:
        return None
    from_name, from_addr = email.utils.parseaddr(str(from_hdr))
    if not from_addr:
        return None

    body = _extract_body(msg)
    if not body.strip():
        return None

    subject = str(msg.get("Subject") or "")
    message_id = msg.get("Message-ID")
    in_reply_to = msg.get("In-Reply-To")
    references = msg.get("References")

    return InboundEvent(
        platform="email",
        user_id=from_addr,
        channel_id=from_addr,
        text=body.strip(),
        timestamp=_header_timestamp(msg),
        metadata={
            "surface": "email",
            "subject": subject,
            "message_id": str(message_id) if message_id else None,
            "in_reply_to": str(in_reply_to) if in_reply_to else None,
            "references": str(references) if references else None,
            "from_name": from_name or None,
            "to": str(msg.get("To") or "") or None,
            "cc": str(msg.get("Cc") or "") or None,
        },
    )


def _header_timestamp(msg: EmailMessage) -> float:
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = email.utils.parsedate_to_datetime(str(date_hdr))
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            pass
    return time.time()


# ---------------------------------------------------------------------------
# Outbound sender — SMTP with threading
# ---------------------------------------------------------------------------

@dataclass
class EmailSendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None


class EmailSender:
    """Builds and sends threaded reply emails over SMTP.

    Inject `_transport` (a callable taking an EmailMessage) for tests; the
    real path opens an SMTP connection, optionally STARTTLS + login, and
    send_message().
    """

    def __init__(
        self,
        *,
        from_addr: str,
        smtp_host: str = "localhost",
        smtp_port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        msgid_domain: str | None = None,
        _transport: Callable[[EmailMessage], None] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not from_addr:
            raise ValueError("EmailSender requires a from_addr")
        self._from_addr = from_addr
        self._host = smtp_host
        self._port = smtp_port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._msgid_domain = msgid_domain or (from_addr.split("@", 1)[-1] or None)
        self._transport = _transport
        self._timeout = timeout
        self.sent_log: list[EmailMessage] = []

    def build_reply(self, event: InboundEvent, body_text: str) -> EmailMessage:
        """Construct a threaded reply EmailMessage from an inbound event."""
        meta = event.metadata or {}
        msg = EmailMessage()
        msg["From"] = self._from_addr
        msg["To"] = event.user_id
        msg["Subject"] = re_subject(meta.get("subject"))

        orig_mid = meta.get("message_id")
        if orig_mid:
            msg["In-Reply-To"] = orig_mid
        refs = build_references(meta.get("references"), orig_mid)
        if refs:
            msg["References"] = refs

        new_mid = email.utils.make_msgid(domain=self._msgid_domain)
        msg["Message-ID"] = new_mid
        msg.set_content(body_text)
        return msg

    def _smtp_send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
            if self._use_tls:
                server.starttls()
            if self._username:
                server.login(self._username, self._password or "")
            server.send_message(msg)

    async def send_reply(self, event: InboundEvent, body_text: str) -> EmailSendResult:
        msg = self.build_reply(event, body_text)
        transport = self._transport or self._smtp_send
        try:
            await asyncio.to_thread(transport, msg)
        except Exception as exc:  # noqa: BLE001
            return EmailSendResult(ok=False, error=str(exc))
        self.sent_log.append(msg)
        return EmailSendResult(ok=True, message_id=msg.get("Message-ID"))

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        """Router on_response hook — sends a threaded reply for the response text."""
        if not response.text:
            return
        await self.send_reply(event, response.text)


# ---------------------------------------------------------------------------
# Inbound poller — IMAP UNSEEN fetch
# ---------------------------------------------------------------------------

class EmailPoller:
    """Fetches UNSEEN messages from an IMAP mailbox and parses them to events.

    The IMAP client is created via `_client_factory()` so tests can inject a
    fake that mimics the imaplib.IMAP4 surface used here: login, select,
    search, fetch, store, logout. The real factory builds an IMAP4_SSL client.
    """

    def __init__(
        self,
        *,
        imap_host: str = "localhost",
        imap_port: int = 993,
        username: str = "",
        password: str = "",
        mailbox: str = "INBOX",
        mark_seen: bool = True,
        _client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._host = imap_host
        self._port = imap_port
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._mark_seen = mark_seen
        self._client_factory = _client_factory

    def _connect(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import imaplib
        client = imaplib.IMAP4_SSL(self._host, self._port)
        client.login(self._username, self._password)
        return client

    def poll_unseen(self) -> list[InboundEvent]:
        """Fetch + parse all UNSEEN messages, marking them seen as we go."""
        client = self._connect()
        events: list[InboundEvent] = []
        try:
            client.select(self._mailbox)
            typ, data = client.search(None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return events
            for num in data[0].split():
                typ, msg_data = client.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data:
                    continue
                raw = _extract_rfc822(msg_data)
                if raw is None:
                    continue
                ev = parse_email(raw)
                if ev is not None:
                    events.append(ev)
                if self._mark_seen:
                    client.store(num, "+FLAGS", "\\Seen")
        finally:
            with contextlib.suppress(Exception):
                client.logout()
        return events


def _extract_rfc822(msg_data: Any) -> bytes | None:
    """Pull the raw message bytes out of an imaplib FETCH response structure."""
    for part in msg_data:
        if isinstance(part, tuple) and len(part) >= 2:
            payload = part[1]
            if isinstance(payload, (bytes, bytearray)):
                return bytes(payload)
            if isinstance(payload, str):
                return payload.encode("utf-8")
    return None


# ---------------------------------------------------------------------------
# Session store — per-sender-address continuity
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_sessions (
    user_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_last_seen ON email_sessions(last_seen);
"""


@dataclass
class _EmailSessionRow:
    user_id: str
    session_id: str
    last_seen: float


class EmailSessionStore:
    """Per-sender-address session store with a 7-day default TTL.

    Email conversations span days, so the window is wider than chat adapters.
    A new message from a known address within the window continues the same
    Sera Session.
    """

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or EMAIL_SESSIONS_DB
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

    def _lookup(self, user_id: str) -> _EmailSessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, session_id, last_seen FROM email_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return _EmailSessionRow(row["user_id"], row["session_id"], float(row["last_seen"]))

    def _upsert(self, user_id: str, session_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO email_sessions (user_id, session_id, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen",
                (user_id, session_id, when),
            )
            con.commit()

    def _touch(self, user_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE email_sessions SET last_seen = ? WHERE user_id = ?",
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
            log.warning("email: session %s gone, recreating for %s",
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
                "SELECT COUNT(*) FROM email_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
