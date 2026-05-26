"""iMessage gateway adapter — local chat.db poll + osascript send.

OUTCLASS: Zero relay, zero cloud, zero third-party. Reads
~/Library/Messages/chat.db directly (macOS Full Disk Access required) and
sends via osascript. Beeper / BlueBubbles / Texts.app all require a running
relay server or a cloud account. Sera needs none of those — it runs on the
same Mac that already has iMessage configured.

Three decisions rivals skip:
  1. ROWID cursor polling — immune to clock drift, no duplicate delivery.
  2. Tapback filter — associated_message_type != 0 (❤️ 👍 reactions) are
     dropped before they touch the agent. Without this every tapback becomes
     an LLM call.
  3. Nanosecond/second epoch auto-detect — Big Sur+ stores date in
     nanoseconds (value > 1e12); older versions use seconds. Both work.

Wire-up:
    store  = iMessageSessionStore()
    sender = iMessageSender()
    router = Router(llm_factory=..., on_response=sender.reply_hook,
                    session_resolver=store.resolver(workspace="..."))
    poller = iMessagePoller(reader=iMessageReader(), router=router)
    await poller.start()   # runs until cancelled
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator

from sera.config import SERA_HOME
from sera.gateway.router import InboundEvent, OutboundResponse
from sera.memory.session import Session

log = logging.getLogger("sera.gateway.imessage")

# macOS Cocoa epoch: seconds between Unix 1970-01-01 and Cocoa 2001-01-01
_COCOA_EPOCH = 978_307_200

IMESSAGE_SESSIONS_DB = SERA_HOME / "imessage_sessions.db"
DEFAULT_SESSION_TTL_S: int = 24 * 3600
DEFAULT_DB_PATH: Path = Path.home() / "Library/Messages/chat.db"
_MAX_PER_POLL = 100

# Exported for test fixture creation.
CHAT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS handle (
    ROWID   INTEGER PRIMARY KEY,
    id      TEXT NOT NULL,
    service TEXT NOT NULL DEFAULT 'iMessage'
);
CREATE TABLE IF NOT EXISTS chat (
    ROWID            INTEGER PRIMARY KEY,
    chat_identifier  TEXT NOT NULL,
    service_name     TEXT NOT NULL DEFAULT 'iMessage',
    display_name     TEXT
);
CREATE TABLE IF NOT EXISTS message (
    ROWID                   INTEGER PRIMARY KEY,
    text                    TEXT,
    date                    INTEGER NOT NULL DEFAULT 0,
    is_from_me              INTEGER NOT NULL DEFAULT 0,
    handle_id               INTEGER,
    service                 TEXT NOT NULL DEFAULT 'iMessage',
    associated_message_type INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (handle_id) REFERENCES handle(ROWID)
);
CREATE TABLE IF NOT EXISTS chat_message_join (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
"""

_POLL_QUERY = """
SELECT
    m.ROWID                   AS rowid,
    m.text                    AS text,
    m.date                    AS date,
    h.id                      AS handle,
    m.service                 AS service
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.ROWID > ?
  AND m.is_from_me = 0
  AND m.text IS NOT NULL
  AND m.text != ''
  AND (m.associated_message_type IS NULL OR m.associated_message_type = 0)
ORDER BY m.ROWID ASC
LIMIT ?
"""


# ---------------------------------------------------------------------------
# Epoch helpers
# ---------------------------------------------------------------------------

def cocoa_to_unix(cocoa: float) -> float:
    """Convert a chat.db `date` value to a Unix timestamp.

    Big Sur+ stores nanoseconds since Cocoa epoch (value > 1e12).
    Older macOS stores seconds since Cocoa epoch.
    """
    if cocoa > 1e12:
        return cocoa / 1e9 + _COCOA_EPOCH
    return cocoa + _COCOA_EPOCH


# ---------------------------------------------------------------------------
# AppleScript send helper
# ---------------------------------------------------------------------------

def _escape_applescript(text: str) -> str:
    """Escape text for embedding in an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


RunnerResult = tuple[int, str]   # (returncode, stderr)
RunnerType = Callable[[str], RunnerResult]


def _default_runner(script: str) -> RunnerResult:
    try:
        proc = subprocess.run(
            ["osascript"],
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=15.0,
        )
        return proc.returncode, proc.stderr.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _build_send_script(handle: str, text: str) -> str:
    h = _escape_applescript(handle)
    t = _escape_applescript(text)
    return (
        'tell application "Messages"\n'
        '    set targetService to 1st service whose service type = iMessage\n'
        f'    set targetBuddy to buddy "{h}" of targetService\n'
        f'    send "{t}" to targetBuddy\n'
        'end tell\n'
    )


# ---------------------------------------------------------------------------
# Reader — polls chat.db with a ROWID cursor
# ---------------------------------------------------------------------------

class iMessageReader:
    """Polls ~/Library/Messages/chat.db for inbound messages newer than last_rowid.

    Filtered:
    - is_from_me = 1 (our own messages)
    - empty / NULL text (media-only messages)
    - associated_message_type != 0 (tapbacks: ❤️ 👍 etc.)
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        last_rowid: int = 0,
        max_per_poll: int = _MAX_PER_POLL,
    ) -> None:
        self._db = db_path or DEFAULT_DB_PATH
        self.last_rowid = last_rowid
        self._max = max_per_poll

    def poll(self) -> list[InboundEvent]:
        """Return InboundEvents for messages newer than last_rowid.

        Advances last_rowid so repeated calls don't duplicate events.
        Returns [] if DB is missing or unreadable (Full Disk Access not granted).
        """
        if not self._db.exists():
            log.debug("iMessage DB not found at %s — Full Disk Access required", self._db)
            return []
        events: list[InboundEvent] = []
        try:
            con = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(_POLL_QUERY, (self.last_rowid, self._max)).fetchall()
            con.close()
        except sqlite3.Error as exc:
            log.warning("iMessage DB read error: %s", exc)
            return []

        for row in rows:
            handle = row["handle"] or "unknown"
            ts = cocoa_to_unix(float(row["date"]))
            events.append(InboundEvent(
                platform="imessage",
                user_id=handle,
                channel_id=handle,
                text=row["text"],
                timestamp=ts,
                metadata={
                    "surface": "imessage",
                    "rowid": row["rowid"],
                    "service": row["service"],
                },
            ))
            if row["rowid"] > self.last_rowid:
                self.last_rowid = row["rowid"]
        return events


# ---------------------------------------------------------------------------
# Sender — osascript / AppleScript
# ---------------------------------------------------------------------------

@dataclass
class iMessageSendResult:
    ok: bool
    handle: str
    error: str | None = None


class iMessageSender:
    """Sends iMessages via osascript.

    Inject `_runner` (str) -> (returncode, stderr) for tests.
    """

    def __init__(self, *, _runner: RunnerType | None = None) -> None:
        self._runner: RunnerType = _runner or _default_runner
        self.sent_log: list[dict[str, Any]] = []

    async def send(self, handle: str, text: str) -> iMessageSendResult:
        if not handle or not text:
            return iMessageSendResult(ok=False, handle=handle, error="empty handle or text")
        script = _build_send_script(handle, text)
        rc, stderr = await asyncio.to_thread(self._runner, script)
        entry: dict[str, Any] = {"handle": handle, "text": text, "rc": rc}
        self.sent_log.append(entry)
        if rc != 0:
            err = stderr.strip() or f"osascript exited {rc}"
            log.warning("iMessage send failed to %s: %s", handle, err)
            return iMessageSendResult(ok=False, handle=handle, error=err)
        return iMessageSendResult(ok=True, handle=handle)

    async def reply_hook(self, event: InboundEvent, response: OutboundResponse) -> None:
        if not response.text:
            return
        await self.send(event.channel_id, response.text)


# ---------------------------------------------------------------------------
# Session store — per-handle 24h continuity
# ---------------------------------------------------------------------------

_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS imessage_sessions (
    handle      TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ims_last_seen ON imessage_sessions(last_seen);
"""


@dataclass
class _SessionRow:
    handle: str
    session_id: str
    last_seen: float


class iMessageSessionStore:
    """Per-handle session store with 24h continuity."""

    def __init__(
        self,
        *,
        db: Path | None = None,
        ttl_s: int = DEFAULT_SESSION_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db or IMESSAGE_SESSIONS_DB
        self._ttl_s = ttl_s
        self._clock = clock
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SESSION_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def _lookup(self, handle: str) -> _SessionRow | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT handle, session_id, last_seen FROM imessage_sessions WHERE handle = ?",
                (handle,),
            ).fetchone()
        if row is None:
            return None
        return _SessionRow(row["handle"], row["session_id"], float(row["last_seen"]))

    def _upsert(self, handle: str, session_id: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO imessage_sessions (handle, session_id, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(handle) DO UPDATE SET "
                "session_id = excluded.session_id, last_seen = excluded.last_seen",
                (handle, session_id, when),
            )
            con.commit()

    def _touch(self, handle: str, when: float) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE imessage_sessions SET last_seen = ? WHERE handle = ?",
                (when, handle),
            )
            con.commit()

    def get_or_create(self, handle: str, *, workspace: str = "/tmp") -> Session:
        now = self._clock()
        existing = self._lookup(handle)
        if existing is not None and (now - existing.last_seen) <= self._ttl_s:
            session = Session.load(existing.session_id)
            if session is not None:
                self._touch(handle, now)
                return session
            log.warning("iMessage: session %s gone, recreating for %s",
                        existing.session_id, handle)
        session = Session.create(workspace=workspace)
        self._upsert(handle, session.id, now)
        return session

    def resolver(self, *, workspace: str = "/tmp") -> Callable[[InboundEvent], Session]:
        def _resolve(event: InboundEvent) -> Session:
            return self.get_or_create(event.user_id, workspace=workspace)
        return _resolve

    def session_id_for(self, handle: str) -> str | None:
        row = self._lookup(handle)
        if row is None or (self._clock() - row.last_seen) > self._ttl_s:
            return None
        return row.session_id

    def active_count(self) -> int:
        cutoff = self._clock() - self._ttl_s
        with self._conn() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM imessage_sessions WHERE last_seen >= ?",
                (cutoff,),
            ).fetchone()[0])


# ---------------------------------------------------------------------------
# Poller — wraps reader into an async loop
# ---------------------------------------------------------------------------

class iMessagePoller:
    """Polls iMessageReader on an interval and dispatches events to Router."""

    def __init__(
        self,
        *,
        reader: iMessageReader,
        router: Any,   # sera.gateway.router.Router — avoid circular import
        interval_s: float = 5.0,
    ) -> None:
        self._reader = reader
        self._router = router
        self._interval = interval_s
        self._running = False

    async def start(self, workspace: str = "/tmp") -> None:
        self._running = True
        log.info("iMessage poller started (interval=%.1fs)", self._interval)
        while self._running:
            try:
                events = self._reader.poll()
                for event in events:
                    try:
                        await self._router.dispatch(event)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("iMessage dispatch error: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("iMessage poll error: %s", exc)
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
        log.info("iMessage poller stopped")
