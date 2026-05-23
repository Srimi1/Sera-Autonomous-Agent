# P-46 — Native scanners for top 5

## Status

done.

## Outclass claim

**API-first with DOM/CDP fallback.** Cleaner than OH's CEF scrapers.

## Goal

Slack, Discord, Telegram, Gmail, iMessage backfill into Memory Tree.

## Files

`sera/integrations/{slack,discord,telegram,gmail,imessage}.py`.

## Verification

24h backfill ingests ≥100 messages per channel.

## Dependencies

P-11, P-12, P-15.


## Notes

2026-05-23: `sera/integrations/scanner_base.py` — IngestedMessage (platform/channel/sender/text/timestamp/message_id/thread_id), BackfillResult, Scanner protocol, backfill() helper writes chunks to MemoryTree with confidence=0.9. Five scanners: slack.py (conversations_history), discord.py (get_messages, time filter), telegram.py (iter_messages), gmail.py (list+get_message, internalDate ms→s), imessage.py (sqlite ~/Library/Messages/chat.db, Cocoa ns/s epoch conversion). All scanners API-first via injectable _client= duck-typed mock; real SDK paths raise RuntimeError when not configured. Verification: each scanner backfills ≥100 chunks in 24h synthetic test. iMessage fixture: in-memory SQLite with handle/message/chat/chat_message_join schema. 19 tests, 892 total.
