"""iMessage scanner — reads ~/Library/Messages/chat.db (macOS).

No API for iMessage. The macOS Messages app stores chats in a SQLite DB at
~/Library/Messages/chat.db. We read it directly (requires Full Disk Access
granted to the terminal in System Preferences).

For tests, pass `db_path=` to point at a fixture database.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from sera.integrations.scanner_base import IngestedMessage

log = logging.getLogger("sera.integrations.imessage")

# macOS Cocoa reference date: seconds since 2001-01-01 UTC
_COCOA_EPOCH_OFFSET = 978_307_200


def _cocoa_to_unix(cocoa_ns_or_s: float) -> float:
    """Convert chat.db `date` (nanoseconds since Cocoa epoch) to unix seconds."""
    # macOS Sierra+ uses nanoseconds; older versions use seconds. Heuristic: ns is huge.
    if cocoa_ns_or_s > 1e12:
        return cocoa_ns_or_s / 1e9 + _COCOA_EPOCH_OFFSET
    return cocoa_ns_or_s + _COCOA_EPOCH_OFFSET


class IMessageScanner:
    platform = "imessage"

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        handles: list[str] | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else Path.home() / "Library/Messages/chat.db"
        self._handles = handles or []

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]:
        if not self._db_path.exists():
            log.warning("iMessage db not found at %s", self._db_path)
            return []

        # Convert unix `since` to cocoa nanoseconds for the query
        cocoa_since_ns = int((since - _COCOA_EPOCH_OFFSET) * 1e9)

        out: list[IngestedMessage] = []
        try:
            con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.execute(
                """
                SELECT
                    m.ROWID                   AS rowid,
                    m.text                    AS text,
                    m.date                    AS date,
                    m.is_from_me              AS is_from_me,
                    h.id                      AS handle,
                    c.chat_identifier         AS chat
                FROM message m
                LEFT JOIN handle h     ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c       ON c.ROWID = cmj.chat_id
                WHERE m.date >= ? AND m.text IS NOT NULL
                ORDER BY m.date DESC
                LIMIT ?
                """,
                (cocoa_since_ns, max_messages),
            )
            for row in cur:
                handle = row["handle"] or ("me" if row["is_from_me"] else "unknown")
                if self._handles and handle not in self._handles:
                    continue
                ts = _cocoa_to_unix(float(row["date"]))
                out.append(IngestedMessage(
                    platform=self.platform,
                    channel=row["chat"] or handle,
                    sender="me" if row["is_from_me"] else handle,
                    text=row["text"] or "",
                    timestamp=ts,
                    message_id=str(row["rowid"]),
                ))
            con.close()
        except sqlite3.Error as exc:
            log.warning("iMessage db read failed: %s", exc)
        return out
