#!/usr/bin/env python3
"""Sera Telegram bot — long-polling mode (no public URL needed).

Usage:
    source .venv/bin/activate
    TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... python scripts/sera_telegram_poll.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request

# Ensure the project root is on sys.path so `sera` is importable
# even when running the script directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sera.gateway.platforms.telegram import (
    TelegramSender,
    TelegramSessionStore,
    parse_telegram,
)
from sera.gateway.router import InboundEvent, Router
from sera.llm.adapters.anthropic_adapter import AnthropicAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-5s %(message)s",
)
log = logging.getLogger("sera.telegram.poll")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    # Try loading from ~/.hermes/.env
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not TOKEN:
    print("TELEGRAM_BOT_TOKEN not set. Export it or add it to ~/.hermes/.env")
    sys.exit(1)

API = f"https://api.telegram.org/bot{TOKEN}"


def tg_request(method: str, params: dict | None = None, timeout: float = 60) -> dict:
    url = f"{API}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def main() -> None:
    # Verify token with getMe
    me = tg_request("getMe")
    if not me.get("ok"):
        print(f"getMe failed: {me}")
        sys.exit(1)
    bot_name = me["result"].get("username", "sera")
    log.info("Bot online: @%s", bot_name)

    # Build Sera components
    sender = TelegramSender(bot_token=TOKEN)
    session_store = TelegramSessionStore()

    def llm_factory(profile: str):
        return AnthropicAdapter(model="claude-sonnet-4-6")

    router = Router(
        llm_factory=llm_factory,
        on_response=sender.reply_hook,
        session_resolver=session_store.resolver(workspace="/tmp"),
        max_iterations=12,
    )

    log.info("Sera router ready. Listening for messages...")

    offset = 0
    running = True

    def stop(*_):
        nonlocal running
        running = False
        log.info("Shutting down...")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        try:
            updates = await asyncio.to_thread(
                tg_request, "getUpdates",
                {"offset": offset, "timeout": 30, "allowed_updates": ["message", "edited_message"]},
                timeout=45,
            )
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as e:
            if "timed out" in str(e).lower():
                continue
            log.warning("Polling error: %s — retrying in 5s", e)
            await asyncio.sleep(5)
            continue

        if not updates.get("ok"):
            log.warning("getUpdates not ok: %s", updates)
            await asyncio.sleep(5)
            continue

        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            event = parse_telegram(update)
            if event is None:
                continue
            username = (update.get("message") or update.get("edited_message", {})).get("from", {}).get("username", "?")
            log.info("Message from @%s: %s", username, event.text[:80])

            try:
                response = await router.dispatch(event)
                if response.ok:
                    log.info("Replied (%dms, profile=%s)", response.latency_ms, response.profile_used)
                else:
                    log.warning("Dispatch error: %s", response.error)
            except Exception:
                log.exception("Unhandled error dispatching message")

    log.info("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
