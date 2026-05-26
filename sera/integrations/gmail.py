"""Gmail scanner — Gmail API (users.messages.list + users.messages.get).

API client duck-types `client.list_messages(q=...)` and `client.get_message(id)`.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from sera.integrations.scanner_base import IngestedMessage

log = logging.getLogger("sera.integrations.gmail")


class GmailScanner:
    platform = "gmail"

    def __init__(
        self,
        *,
        oauth_token: str | None = None,
        mailbox: str = "inbox",
        _client: Any = None,
    ) -> None:
        self._token = oauth_token or os.environ.get("GMAIL_OAUTH_TOKEN", "")
        self._mailbox = mailbox
        self._client = _client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        raise RuntimeError("Gmail client not configured. Pass _client= or install google-api-python-client.")

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]:
        client = self._get_client()
        # Gmail query: after:<unix_ts>
        query = f"after:{int(since)}"
        out: list[IngestedMessage] = []
        try:
            listings = client.list_messages(q=query)
        except Exception as exc:  # noqa: BLE001
            log.warning("gmail list failed: %s", exc)
            return out

        for entry in listings[:max_messages]:
            try:
                msg_id = str(entry.get("id") or "") if isinstance(entry, dict) else str(entry)
                m = client.get_message(msg_id)
                if not isinstance(m, dict):
                    continue
                headers = m.get("headers", {}) if isinstance(m.get("headers"), dict) else {
                    h.get("name", "").lower(): h.get("value", "")
                    for h in m.get("payload", {}).get("headers", [])
                }
                sender = headers.get("from", "unknown")
                subject = headers.get("subject", "")
                ts_ms = int(m.get("internalDate", "0") or 0)
                ts = ts_ms / 1000.0 if ts_ms else time.time()
                body = m.get("snippet") or m.get("body", "")
                out.append(IngestedMessage(
                    platform=self.platform,
                    channel=self._mailbox,
                    sender=sender,
                    text=f"Subject: {subject}\n\n{body}",
                    timestamp=ts,
                    message_id=msg_id,
                    thread_id=m.get("threadId"),
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("gmail message fetch failed: %s", exc)
        return out
