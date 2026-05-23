"""Discord scanner — Discord REST API (channels/{id}/messages).

API client duck-types `client.get_messages(channel_id, before=None, limit=100)`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sera.integrations.scanner_base import IngestedMessage

log = logging.getLogger("sera.integrations.discord")


class DiscordScanner:
    platform = "discord"

    def __init__(
        self,
        *,
        token: str | None = None,
        channels: list[str] | None = None,
        _client: Any = None,
    ) -> None:
        self._token = token or os.environ.get("DISCORD_BOT_TOKEN", "")
        self._channels = channels or []
        self._client = _client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        raise RuntimeError("Discord client not configured. Pass _client= or install a discord library.")

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]:
        client = self._get_client()
        out: list[IngestedMessage] = []
        for channel_id in self._channels:
            try:
                messages = client.get_messages(channel_id, limit=min(max_messages, 100))
                for m in messages:
                    ts = float(m.get("timestamp_unix") or m.get("timestamp", since))
                    if ts < since:
                        continue
                    if len(out) >= max_messages:
                        break
                    out.append(IngestedMessage(
                        platform=self.platform,
                        channel=channel_id,
                        sender=m.get("author", {}).get("username", "unknown") if isinstance(m.get("author"), dict) else str(m.get("author", "unknown")),
                        text=m.get("content", ""),
                        timestamp=ts,
                        message_id=str(m.get("id", "")),
                    ))
            except Exception as exc:  # noqa: BLE001
                log.warning("discord channel %s fetch failed: %s", channel_id, exc)
        return out
