"""Slack scanner — Slack Web API (conversations.history) with DOM fallback.

API client duck-types `client.conversations_history(channel, oldest, limit)` and
`client.conversations_list()`. Mock clients (or the real `slack_sdk` WebClient)
both work.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sera.integrations.scanner_base import IngestedMessage

log = logging.getLogger("sera.integrations.slack")


class SlackScanner:
    platform = "slack"

    def __init__(
        self,
        *,
        token: str | None = None,
        channels: list[str] | None = None,
        _client: Any = None,
    ) -> None:
        self._token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self._channels = channels or []
        self._client = _client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from slack_sdk import WebClient  # type: ignore[import]
            return WebClient(token=self._token)
        except ImportError as e:
            raise RuntimeError("slack_sdk not installed: pip install slack-sdk") from e

    async def fetch(
        self,
        *,
        since: float,
        max_messages: int = 1000,
    ) -> list[IngestedMessage]:
        client = self._get_client()
        channels = self._channels or self._discover_channels(client)
        out: list[IngestedMessage] = []
        for channel in channels:
            try:
                resp = client.conversations_history(
                    channel=channel,
                    oldest=str(since),
                    limit=min(max_messages, 200),
                )
                msgs = resp.get("messages", []) if isinstance(resp, dict) else getattr(resp, "data", {}).get("messages", [])
                for m in msgs:
                    if len(out) >= max_messages:
                        break
                    out.append(IngestedMessage(
                        platform=self.platform,
                        channel=channel,
                        sender=m.get("user", m.get("username", "unknown")),
                        text=m.get("text", ""),
                        timestamp=float(m.get("ts", since)),
                        message_id=m.get("client_msg_id") or m.get("ts", ""),
                        thread_id=m.get("thread_ts"),
                    ))
            except Exception as exc:  # noqa: BLE001
                log.warning("slack channel %s fetch failed: %s", channel, exc)
        return out

    @staticmethod
    def _discover_channels(client: Any) -> list[str]:
        try:
            resp = client.conversations_list()
            channels = resp.get("channels", []) if isinstance(resp, dict) else []
            return [c["id"] for c in channels if c.get("id")]
        except Exception:  # noqa: BLE001
            return []
