"""Unified cross-channel identity — one person, many channels, one session.

OUTCLASS: every rival siloes sessions by channel. Telegram bot remembers
Telegram. Slack bot remembers Slack. They never join. Sera maps a person's
channel handles to a single Identity, and that Identity owns ONE Session. Ask
on Telegram, follow up on Slack — same session, context preserved.

Two capabilities no rival ships together:

  1. One session DB across every channel. The per-platform stores (P-52..P-58)
     keyed sessions by the platform's own user_id. This layer keys sessions by
     identity_id, and routes (platform, channel_user_id) → identity_id first.
     Link the handles once; the conversation is continuous across all of them.

  2. Privacy-first reply routing (native > cloud). Each platform carries a
     PrivacyTier. When an identity is reachable on several channels,
     `preferred_channel` returns the most-private one — iMessage (NATIVE,
     local-only) is chosen over Telegram (CLOUD) for the outbound reply.

Drop-in: `store.resolver()` is a Router `session_resolver`, exactly like the
per-platform stores it replaces.

Wire-up:
    store = IdentityStore()
    owner = store.create_identity(display_name="me")
    store.link_all(owner, [("telegram", "42"), ("slack", "U123"), ("imessage", "+1415...")])
    router = Router(llm_factory=..., session_resolver=store.resolver(workspace="..."))
    # Now a Telegram message and a Slack message from those handles share one Session.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Callable, Generator

from sera.config import SERA_HOME
from sera.gateway.router import InboundEvent
from sera.memory.session import Session

log = logging.getLogger("sera.gateway.identity")

IDENTITY_DB = SERA_HOME / "identity.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600


# ---------------------------------------------------------------------------
# Privacy tiers — native beats cloud
# ---------------------------------------------------------------------------

class PrivacyTier(IntEnum):
    """Lower is more private. Used to rank outbound channels."""
    NATIVE = 0       # local-only, zero relay (iMessage: chat.db + osascript)
    SELF_HOSTED = 1  # your own infra (email IMAP/SMTP, WhatsApp local bridge)
    CLOUD = 2        # third-party servers (telegram, discord, slack, twilio SMS)


# Default platform → privacy tier. Callers can override per-store.
PLATFORM_PRIVACY: dict[str, PrivacyTier] = {
    "imessage": PrivacyTier.NATIVE,
    "email": PrivacyTier.SELF_HOSTED,
    "whatsapp": PrivacyTier.SELF_HOSTED,
    "telegram": PrivacyTier.CLOUD,
    "discord": PrivacyTier.CLOUD,
    "slack": PrivacyTier.CLOUD,
    "twilio": PrivacyTier.CLOUD,
}

_UNKNOWN_TIER = PrivacyTier.CLOUD   # unknown platform → treat as least private


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelLink:
    platform: str
    channel_user_id: str
    last_seen: float


@dataclass(frozen=True)
class Identity:
    identity_id: str
    display_name: str | None
    links: list[ChannelLink]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    identity_id  TEXT PRIMARY KEY,
    display_name TEXT,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_links (
    platform        TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    identity_id     TEXT NOT NULL,
    last_seen       REAL NOT NULL,
    PRIMARY KEY (platform, channel_user_id)
);
CREATE TABLE IF NOT EXISTS identity_sessions (
    identity_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_identity ON channel_links(identity_id);
"""


# ---------------------------------------------------------------------------
# IdentityStore
# ---------------------------------------------------------------------------

class IdentityStore:
    """The unification layer: channel handle → identity → one shared session."""

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
        privacy_map: dict[str, PrivacyTier] | None = None,
    ) -> None:
        self._db = db or IDENTITY_DB
        self._ttl_s = ttl_s
        self._clock = clock
        self._privacy = privacy_map if privacy_map is not None else dict(PLATFORM_PRIVACY)
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

    # ------------------------------------------------------------------
    # Identity management
    # ------------------------------------------------------------------

    def create_identity(self, *, display_name: str | None = None) -> str:
        identity_id = uuid.uuid4().hex
        with self._conn() as con:
            con.execute(
                "INSERT INTO identities (identity_id, display_name, created_at) VALUES (?, ?, ?)",
                (identity_id, display_name, self._clock()),
            )
            con.commit()
        return identity_id

    def identity_exists(self, identity_id: str) -> bool:
        with self._conn() as con:
            return con.execute(
                "SELECT 1 FROM identities WHERE identity_id = ?", (identity_id,)
            ).fetchone() is not None

    def link(self, identity_id: str, platform: str, channel_user_id: str) -> None:
        """Attach a channel handle to an identity.

        If the handle was already linked to a different identity, it is
        reassigned to `identity_id` (an explicit link is authoritative).
        """
        if not self.identity_exists(identity_id):
            raise ValueError(f"unknown identity_id: {identity_id}")
        now = self._clock()
        with self._conn() as con:
            con.execute(
                "INSERT INTO channel_links (platform, channel_user_id, identity_id, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(platform, channel_user_id) DO UPDATE SET "
                "identity_id = excluded.identity_id",
                (platform, channel_user_id, identity_id, now),
            )
            con.commit()

    def link_all(self, identity_id: str, handles: list[tuple[str, str]]) -> None:
        for platform, channel_user_id in handles:
            self.link(identity_id, platform, channel_user_id)

    def unlink(self, platform: str, channel_user_id: str) -> None:
        with self._conn() as con:
            con.execute(
                "DELETE FROM channel_links WHERE platform = ? AND channel_user_id = ?",
                (platform, channel_user_id),
            )
            con.commit()

    def identity_for(self, platform: str, channel_user_id: str) -> str | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT identity_id FROM channel_links WHERE platform = ? AND channel_user_id = ?",
                (platform, channel_user_id),
            ).fetchone()
        return row["identity_id"] if row else None

    def links_for(self, identity_id: str) -> list[ChannelLink]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT platform, channel_user_id, last_seen FROM channel_links "
                "WHERE identity_id = ? ORDER BY last_seen DESC",
                (identity_id,),
            ).fetchall()
        return [ChannelLink(r["platform"], r["channel_user_id"], float(r["last_seen"])) for r in rows]

    def get_identity(self, identity_id: str) -> Identity | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT identity_id, display_name FROM identities WHERE identity_id = ?",
                (identity_id,),
            ).fetchone()
        if row is None:
            return None
        return Identity(row["identity_id"], row["display_name"], self.links_for(identity_id))

    def merge(self, primary_id: str, secondary_id: str) -> None:
        """Fold `secondary_id` into `primary_id`: move all links, keep the
        freshest session, then delete the secondary identity.

        Used when two separate identities turn out to be one person.
        """
        if primary_id == secondary_id:
            return
        if not self.identity_exists(primary_id) or not self.identity_exists(secondary_id):
            raise ValueError("both identities must exist to merge")
        with self._conn() as con:
            con.execute(
                "UPDATE channel_links SET identity_id = ? WHERE identity_id = ?",
                (primary_id, secondary_id),
            )
            # Keep whichever session was seen most recently.
            prim = con.execute(
                "SELECT session_id, last_seen FROM identity_sessions WHERE identity_id = ?",
                (primary_id,),
            ).fetchone()
            sec = con.execute(
                "SELECT session_id, last_seen FROM identity_sessions WHERE identity_id = ?",
                (secondary_id,),
            ).fetchone()
            if sec is not None:
                if prim is None or float(sec["last_seen"]) > float(prim["last_seen"]):
                    con.execute(
                        "INSERT INTO identity_sessions (identity_id, session_id, last_seen) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(identity_id) DO UPDATE SET "
                        "session_id = excluded.session_id, last_seen = excluded.last_seen",
                        (primary_id, sec["session_id"], float(sec["last_seen"])),
                    )
                con.execute("DELETE FROM identity_sessions WHERE identity_id = ?", (secondary_id,))
            con.execute("DELETE FROM identities WHERE identity_id = ?", (secondary_id,))
            con.commit()

    # ------------------------------------------------------------------
    # Session unification — the payoff
    # ------------------------------------------------------------------

    def _touch_link(self, platform: str, channel_user_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE channel_links SET last_seen = ? WHERE platform = ? AND channel_user_id = ?",
                (when, platform, channel_user_id),
            )
            con.commit()

    def _session_row(self, identity_id: str) -> tuple[str, float] | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT session_id, last_seen FROM identity_sessions WHERE identity_id = ?",
                (identity_id,),
            ).fetchone()
        if row is None:
            return None
        return row["session_id"], float(row["last_seen"])

    def _upsert_session(self, identity_id: str, session_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO identity_sessions (identity_id, session_id, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(identity_id) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen",
                (identity_id, session_id, when),
            )
            con.commit()

    def get_or_create_session(
        self,
        platform: str,
        channel_user_id: str,
        *,
        workspace: str = "/tmp",
        auto_create_identity: bool = True,
    ) -> Session:
        """Resolve (platform, handle) to the identity's shared Session.

        New handle + auto_create_identity → a fresh identity is minted so the
        handle gets its own session (safe default: never merge two people).
        Link handles explicitly to make their sessions converge.
        """
        now = self._clock()
        identity_id = self.identity_for(platform, channel_user_id)
        if identity_id is None:
            if not auto_create_identity:
                # Ephemeral session, no persistence.
                return Session.create(workspace=workspace)
            identity_id = self.create_identity()
            self.link(identity_id, platform, channel_user_id)
        else:
            self._touch_link(platform, channel_user_id, now)

        existing = self._session_row(identity_id)
        if existing is not None and (now - existing[1]) <= self._ttl_s:
            session = Session.load(existing[0])
            if session is not None:
                self._upsert_session(identity_id, session.id, now)
                return session
            log.warning("identity: session %s gone, recreating for %s", existing[0], identity_id)

        session = Session.create(workspace=workspace)
        self._upsert_session(identity_id, session.id, now)
        return session

    def session_id_for_identity(self, identity_id: str) -> str | None:
        row = self._session_row(identity_id)
        if row is None or (self._clock() - row[1]) > self._ttl_s:
            return None
        return row[0]

    def session_id_for_channel(self, platform: str, channel_user_id: str) -> str | None:
        identity_id = self.identity_for(platform, channel_user_id)
        if identity_id is None:
            return None
        return self.session_id_for_identity(identity_id)

    def resolver(
        self,
        *,
        workspace: str = "/tmp",
        auto_create_identity: bool = True,
    ) -> Callable[[InboundEvent], Session]:
        """Build a Router session_resolver bound to this store."""
        def _resolve(event: InboundEvent) -> Session:
            return self.get_or_create_session(
                event.platform, event.user_id,
                workspace=workspace,
                auto_create_identity=auto_create_identity,
            )
        return _resolve

    # ------------------------------------------------------------------
    # Privacy-first reply routing — native beats cloud
    # ------------------------------------------------------------------

    def tier_for(self, platform: str) -> PrivacyTier:
        return self._privacy.get(platform, _UNKNOWN_TIER)

    def preferred_channel(self, identity_id: str) -> ChannelLink | None:
        """Return the most-private reachable channel for an identity.

        Ranks by PrivacyTier ascending (NATIVE first); ties broken by most
        recent last_seen. Lets the gateway prefer iMessage over Telegram when
        a person is reachable on both.
        """
        links = self.links_for(identity_id)
        if not links:
            return None
        return min(links, key=lambda lnk: (self.tier_for(lnk.platform), -lnk.last_seen))

    def active_identity_count(self) -> int:
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM identity_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])
