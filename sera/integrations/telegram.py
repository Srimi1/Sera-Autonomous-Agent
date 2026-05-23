"""Telegram scanner — MTProto via Telethon or Bot API.

API client duck-types `client.iter_messages(chat, offset_date=None, limit=N)`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sera.integrations.scanner_base import IngestedMessage

log = logging.getLogger("sera.integrations.telegram")


class TelegramScanner:
    platform = "telegram"

    def __init__(
        self,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        chats: list[str] | None = None,
        _client: Any = None,
    ) -> None:
        self._api_id = api_id or int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
        self._api_hash = api_hash or os.environ.get("TELEGRAM_API_HASH", "")
        self._chats = chats or []
        self._client = _client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        raise RuntimeError("Telegram client not configured. Pass _client= or install telethon.")

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]:
        client = self._get_client()
        out: list[IngestedMessage] = []
        for chat in self._chats:
            try:
                messages = client.iter_messages(chat, limit=max_messages)
                for m in messages:
                    ts = float(m.get("date_unix") or m.get("date", since))
                    if ts < since:
                        continue
                    if len(out) >= max_messages:
                        break
                    out.append(IngestedMessage(
                        platform=self.platform,
                        channel=str(chat),
                        sender=str(m.get("sender_id", m.get("from_id", "unknown"))),
                        text=m.get("message", m.get("text", "")),
                        timestamp=ts,
                        message_id=str(m.get("id", "")),
                    ))
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram chat %s fetch failed: %s", chat, exc)
        return out
