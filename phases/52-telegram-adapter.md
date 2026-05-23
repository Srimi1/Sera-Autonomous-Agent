# P-52 — Telegram adapter

## Status

done.

## Outclass claim

**24h session continuity across messages** by user_id.

## Goal

TG bot.

## Files

`sera/gateway/platforms/telegram.py`.

## Verification

message → reply, 24h gap preserved.

## Dependencies

P-51.


## Notes

2026-05-23: sera/gateway/platforms/telegram.py — parse_telegram(payload) handles message + edited_message, extracts user_id/chat_id/text/message_id/username/chat_type. TelegramSender uses urllib via asyncio.to_thread (no httpx dep added); injectable _poster for tests. reply_hook wired as Router.on_response — auto-replies with reply_to_message_id from inbound metadata. TelegramSessionStore: SQLite at ~/.sera/telegram_sessions.db, user_id → (session_id, last_seen). resolver(workspace) returns a callable Router uses to map InboundEvent → Session. clock injection (clock=lambda: t) for deterministic TTL boundary tests. Router gains session_resolver + on_response hooks; both default to None for backward compat with P-51. Verification: 23h gap → same session; 25h gap → fresh session; full dispatch chain sends reply via sender.reply_hook. 29 tests, 1073 total.
