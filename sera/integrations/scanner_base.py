"""Common scaffolding for native messaging-platform scanners.

Every scanner returns IngestedMessage instances and ingests them into the
Memory Tree via the backfill helper. Scanners follow API-first with a
DOM/CDP fallback when API access isn't configured.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sera.memory.tree import MemoryTree


# ---------------------------------------------------------------------------
# Common message shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestedMessage:
    platform: str           # "slack", "discord", "telegram", "gmail", "imessage"
    channel: str            # channel id / room name / chat id / mailbox / phone
    sender: str             # username, email, phone
    text: str               # plaintext body
    timestamp: float        # unix seconds
    message_id: str         # platform-specific id
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def chunk_content(self) -> str:
        """Format for Memory Tree storage — one line per fact."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.timestamp))
        thread = f" thread={self.thread_id}" if self.thread_id else ""
        return (
            f"[{self.platform}:{self.channel}{thread}] {ts} <{self.sender}>\n"
            f"{self.text}"
        )

    def source_tag(self) -> str:
        """Memory Tree `source` field value."""
        return f"{self.platform}/{self.channel}"


@dataclass
class BackfillResult:
    platform: str
    messages_fetched: int
    chunks_written: int
    duration_s: float
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Scanner protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Scanner(Protocol):
    platform: str

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]: ...


# ---------------------------------------------------------------------------
# Backfill helper — used by every scanner
# ---------------------------------------------------------------------------

async def backfill(
    scanner: Scanner,
    tree: MemoryTree,
    *,
    hours: float = 24.0,
    max_messages: int = 1000,
    confidence: float = 0.9,
) -> BackfillResult:
    """Fetch last `hours` of messages and write each as a Memory Tree chunk."""
    t0 = time.time()
    since = time.time() - hours * 3600.0
    errors: list[str] = []

    try:
        messages = await scanner.fetch(since=since, max_messages=max_messages)
    except Exception as exc:  # noqa: BLE001
        return BackfillResult(
            platform=scanner.platform,
            messages_fetched=0,
            chunks_written=0,
            duration_s=time.time() - t0,
            errors=[f"fetch failed: {exc}"],
        )

    written = 0
    for msg in messages:
        try:
            tree.add_chunk(
                source=msg.source_tag(),
                content=msg.chunk_content(),
                summary=msg.text[:160],
                confidence=confidence,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"chunk write failed ({msg.message_id}): {exc}")

    return BackfillResult(
        platform=scanner.platform,
        messages_fetched=len(messages),
        chunks_written=written,
        duration_s=time.time() - t0,
        errors=errors,
    )
