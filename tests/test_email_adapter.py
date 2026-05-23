"""Tests for sera.gateway.platforms.email — IMAP receive + threaded SMTP reply.

P-56 verification: reply lands in thread — the reply carries Re: subject,
In-Reply-To set to the original Message-ID, and a References chain that
extends the original's.
"""
from __future__ import annotations

import asyncio
from email.message import EmailMessage

import pytest

from sera.gateway.platforms.email import (
    EmailPoller,
    EmailSender,
    EmailSessionStore,
    build_references,
    parse_email,
    re_subject,
)
from sera.gateway.router import OutboundResponse


# ---------------------------------------------------------------------------
# Raw-message fixtures
# ---------------------------------------------------------------------------

def _raw(
    *,
    from_addr: str = "Alice <alice@example.com>",
    to_addr: str = "sera@myhost.com",
    subject: str = "Project update",
    message_id: str = "<orig123@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    body: str = "Can you summarize the latest status?",
    content_type: str = "text/plain",
) -> bytes:
    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        "Date: Mon, 24 May 2026 10:00:00 +0000",
        f"Content-Type: {content_type}; charset=utf-8",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if references:
        headers.append(f"References: {references}")
    raw = "\r\n".join(headers) + "\r\n\r\n" + body
    return raw.encode("utf-8")


def _raw_multipart(*, body_plain: str = "plain body", body_html: str = "<p>html body</p>") -> bytes:
    boundary = "BOUNDARY123"
    raw = (
        "From: Bob <bob@example.com>\r\n"
        "To: sera@myhost.com\r\n"
        "Subject: Multipart test\r\n"
        "Message-ID: <mp1@example.com>\r\n"
        f'Content-Type: multipart/alternative; boundary="{boundary}"\r\n'
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body_plain}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        f"{body_html}\r\n"
        f"--{boundary}--\r\n"
    )
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# re_subject
# ---------------------------------------------------------------------------

def test_re_subject_adds_prefix():
    assert re_subject("Hello") == "Re: Hello"


def test_re_subject_idempotent():
    assert re_subject("Re: Hello") == "Re: Hello"


def test_re_subject_case_insensitive():
    assert re_subject("RE: Hello") == "RE: Hello"
    assert re_subject("re: hello") == "re: hello"


def test_re_subject_empty():
    assert re_subject("") == "Re: (no subject)"
    assert re_subject(None) == "Re: (no subject)"


def test_re_subject_no_double_prefix_with_spaces():
    assert re_subject("  Re:  spaced") == "Re:  spaced"


# ---------------------------------------------------------------------------
# build_references — the threading core
# ---------------------------------------------------------------------------

def test_build_references_first_reply():
    # No prior References → just the parent Message-ID
    assert build_references(None, "<orig@x>") == "<orig@x>"


def test_build_references_extends_chain():
    refs = build_references("<a@x> <b@x>", "<c@x>")
    assert refs == "<a@x> <b@x> <c@x>"


def test_build_references_no_duplicate():
    refs = build_references("<a@x> <b@x>", "<b@x>")
    assert refs == "<a@x> <b@x>"


def test_build_references_empty():
    assert build_references(None, None) == ""


# ---------------------------------------------------------------------------
# parse_email
# ---------------------------------------------------------------------------

def test_parse_basic():
    ev = parse_email(_raw())
    assert ev is not None
    assert ev.platform == "email"
    assert ev.user_id == "alice@example.com"
    assert ev.channel_id == "alice@example.com"
    assert "summarize the latest status" in ev.text
    assert ev.metadata["subject"] == "Project update"
    assert ev.metadata["message_id"] == "<orig123@example.com>"
    assert ev.metadata["from_name"] == "Alice"


def test_parse_threading_headers():
    ev = parse_email(_raw(in_reply_to="<prev@x>", references="<a@x> <prev@x>"))
    assert ev.metadata["in_reply_to"] == "<prev@x>"
    assert ev.metadata["references"] == "<a@x> <prev@x>"


def test_parse_accepts_str():
    ev = parse_email(_raw().decode("utf-8"))
    assert ev is not None
    assert ev.user_id == "alice@example.com"


def test_parse_multipart_prefers_plain():
    ev = parse_email(_raw_multipart(body_plain="the plain text"))
    assert ev is not None
    assert "the plain text" in ev.text


def test_parse_html_stripped_when_no_plain():
    raw = (
        "From: Carol <carol@example.com>\r\n"
        "To: sera@myhost.com\r\n"
        "Subject: HTML only\r\n"
        "Message-ID: <html1@example.com>\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Hello <b>world</b></p></body></html>\r\n"
    ).encode("utf-8")
    ev = parse_email(raw)
    assert ev is not None
    assert "Hello" in ev.text
    assert "<" not in ev.text


def test_parse_no_from_returns_none():
    raw = (
        "To: sera@myhost.com\r\n"
        "Subject: No sender\r\n\r\n"
        "body\r\n"
    ).encode("utf-8")
    assert parse_email(raw) is None


def test_parse_empty_body_returns_none():
    assert parse_email(_raw(body="   ")) is None


def test_parse_date_timestamp():
    ev = parse_email(_raw())
    # Mon, 24 May 2026 10:00:00 +0000
    assert ev.timestamp > 0


# ---------------------------------------------------------------------------
# EmailSender.build_reply — threading verification (the outclass)
# ---------------------------------------------------------------------------

def _sender(captured: list[EmailMessage] | None = None) -> EmailSender:
    sink = captured if captured is not None else []
    return EmailSender(
        from_addr="sera@myhost.com",
        _transport=lambda msg: sink.append(msg),
    )


def test_build_reply_subject_prefixed():
    ev = parse_email(_raw(subject="Quarterly numbers"))
    msg = _sender().build_reply(ev, "Here you go.")
    assert msg["Subject"] == "Re: Quarterly numbers"


def test_build_reply_in_reply_to_set():
    ev = parse_email(_raw(message_id="<orig123@example.com>"))
    msg = _sender().build_reply(ev, "reply body")
    assert msg["In-Reply-To"] == "<orig123@example.com>"


def test_build_reply_references_extends():
    ev = parse_email(_raw(message_id="<orig123@example.com>", references="<a@x> <b@x>"))
    msg = _sender().build_reply(ev, "reply body")
    assert msg["References"] == "<a@x> <b@x> <orig123@example.com>"


def test_build_reply_references_first_reply():
    ev = parse_email(_raw(message_id="<orig123@example.com>", references=None))
    msg = _sender().build_reply(ev, "reply body")
    assert msg["References"] == "<orig123@example.com>"


def test_build_reply_to_and_from():
    ev = parse_email(_raw(from_addr="Alice <alice@example.com>"))
    msg = _sender().build_reply(ev, "body")
    assert msg["To"] == "alice@example.com"
    assert msg["From"] == "sera@myhost.com"


def test_build_reply_generates_message_id():
    ev = parse_email(_raw())
    msg = _sender().build_reply(ev, "body")
    assert msg["Message-ID"]
    assert msg["Message-ID"] != "<orig123@example.com>"
    assert "myhost.com" in msg["Message-ID"]


def test_build_reply_body_content():
    ev = parse_email(_raw())
    msg = _sender().build_reply(ev, "The summary is ready.")
    assert "The summary is ready." in msg.get_content()


# ---------------------------------------------------------------------------
# EmailSender.send_reply / reply_hook
# ---------------------------------------------------------------------------

def test_send_reply_success():
    captured: list[EmailMessage] = []
    sender = _sender(captured)
    ev = parse_email(_raw())
    result = asyncio.run(sender.send_reply(ev, "reply"))
    assert result.ok is True
    assert result.message_id is not None
    assert len(captured) == 1


def test_send_reply_transport_error():
    def _boom(msg):
        raise RuntimeError("smtp down")
    sender = EmailSender(from_addr="sera@myhost.com", _transport=_boom)
    ev = parse_email(_raw())
    result = asyncio.run(sender.send_reply(ev, "reply"))
    assert result.ok is False
    assert "smtp down" in result.error


def test_reply_hook_sends_threaded():
    captured: list[EmailMessage] = []
    sender = _sender(captured)
    ev = parse_email(_raw(message_id="<orig@x>"))
    response = OutboundResponse(event=ev, ok=True, text="hello back")
    asyncio.run(sender.reply_hook(ev, response))
    assert len(captured) == 1
    assert captured[0]["In-Reply-To"] == "<orig@x>"


def test_reply_hook_empty_text_no_op():
    captured: list[EmailMessage] = []
    sender = _sender(captured)
    ev = parse_email(_raw())
    response = OutboundResponse(event=ev, ok=True, text="")
    asyncio.run(sender.reply_hook(ev, response))
    assert captured == []


def test_sender_requires_from_addr():
    with pytest.raises(ValueError, match="from_addr"):
        EmailSender(from_addr="")


# ---------------------------------------------------------------------------
# EmailPoller — IMAP fetch with injected client
# ---------------------------------------------------------------------------

class _FakeImap:
    """Minimal imaplib.IMAP4-shaped fake for poller tests."""

    def __init__(self, messages: dict[bytes, bytes]):
        self._messages = messages   # {seqnum_bytes: raw_rfc822}
        self.selected: str | None = None
        self.stored: list[tuple[bytes, str, str]] = []
        self.logged_out = False

    def select(self, mailbox):
        self.selected = mailbox
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(self._messages.keys())])

    def fetch(self, num, parts):
        raw = self._messages.get(num)
        if raw is None:
            return ("NO", [])
        return ("OK", [(b"%s (RFC822 {%d}" % (num, len(raw)), raw), b")"])

    def store(self, num, flags, value):
        self.stored.append((num, flags, value))
        return ("OK", [b""])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


def test_poller_parses_unseen():
    raw = _raw(from_addr="dave@example.com", body="poll me")
    fake = _FakeImap({b"1": raw})
    poller = EmailPoller(_client_factory=lambda: fake)
    events = poller.poll_unseen()
    assert len(events) == 1
    assert events[0].user_id == "dave@example.com"
    assert "poll me" in events[0].text


def test_poller_marks_seen():
    fake = _FakeImap({b"1": _raw()})
    poller = EmailPoller(_client_factory=lambda: fake)
    poller.poll_unseen()
    assert fake.stored == [(b"1", "+FLAGS", "\\Seen")]


def test_poller_mark_seen_disabled():
    fake = _FakeImap({b"1": _raw()})
    poller = EmailPoller(mark_seen=False, _client_factory=lambda: fake)
    poller.poll_unseen()
    assert fake.stored == []


def test_poller_logs_out():
    fake = _FakeImap({b"1": _raw()})
    poller = EmailPoller(_client_factory=lambda: fake)
    poller.poll_unseen()
    assert fake.logged_out is True


def test_poller_multiple_messages():
    fake = _FakeImap({
        b"1": _raw(from_addr="a@x.com", body="first"),
        b"2": _raw(from_addr="b@x.com", body="second"),
    })
    poller = EmailPoller(_client_factory=lambda: fake)
    events = poller.poll_unseen()
    assert len(events) == 2


def test_poller_no_unseen():
    fake = _FakeImap({})
    poller = EmailPoller(_client_factory=lambda: fake)
    assert poller.poll_unseen() == []


# ---------------------------------------------------------------------------
# EmailSessionStore
# ---------------------------------------------------------------------------

def test_store_creates_session(tmp_path):
    store = EmailSessionStore(db=tmp_path / "e.db")
    session = store.get_or_create("alice@example.com")
    assert store.session_id_for("alice@example.com") == session.id


def test_store_same_session_within_ttl(tmp_path):
    store = EmailSessionStore(db=tmp_path / "e.db", ttl_s=3600)
    s1 = store.get_or_create("alice@example.com")
    s2 = store.get_or_create("alice@example.com")
    assert s1.id == s2.id


def test_store_ttl_expiry(tmp_path):
    t = [0.0]
    store = EmailSessionStore(db=tmp_path / "e.db", ttl_s=100, clock=lambda: t[0])
    s1 = store.get_or_create("alice@example.com")
    t[0] = 101.0
    s2 = store.get_or_create("alice@example.com")
    assert s1.id != s2.id


def test_store_resolver(tmp_path):
    store = EmailSessionStore(db=tmp_path / "e.db")
    resolver = store.resolver(workspace="/tmp")
    ev = parse_email(_raw())
    assert resolver(ev) is not None


def test_store_active_count(tmp_path):
    store = EmailSessionStore(db=tmp_path / "e.db", ttl_s=3600)
    store.get_or_create("a@x.com")
    store.get_or_create("b@x.com")
    assert store.active_count() == 2


def test_store_unknown_returns_none(tmp_path):
    store = EmailSessionStore(db=tmp_path / "e.db")
    assert store.session_id_for("nobody@x.com") is None


# ---------------------------------------------------------------------------
# End-to-end: parse → reply lands in thread (the verification)
# ---------------------------------------------------------------------------

def test_e2e_reply_lands_in_thread():
    captured: list[EmailMessage] = []
    sender = _sender(captured)
    # Original message with an existing thread
    raw = _raw(
        from_addr="Alice <alice@example.com>",
        subject="Re: Deploy plan",
        message_id="<msg2@example.com>",
        references="<msg1@example.com>",
    )
    ev = parse_email(raw)
    response = OutboundResponse(event=ev, ok=True, text="Approved, ship it.")
    asyncio.run(sender.reply_hook(ev, response))

    reply = captured[0]
    # Subject preserved (already had Re:, so no stacking)
    assert reply["Subject"] == "Re: Deploy plan"
    # In-Reply-To points at the message we're replying to
    assert reply["In-Reply-To"] == "<msg2@example.com>"
    # References chain extended: original refs + the parent's Message-ID
    assert reply["References"] == "<msg1@example.com> <msg2@example.com>"
    # Reply addressed back to the sender
    assert reply["To"] == "alice@example.com"
