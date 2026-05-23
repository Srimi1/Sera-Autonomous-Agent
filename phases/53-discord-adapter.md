# P-53 — Discord adapter

## Status

done.

## Outclass claim

**Slash + DM + thread** unified.

## Files

`sera/gateway/platforms/discord.py`.

## Verification

slash command in thread; DM also works.

## Dependencies

P-51.


## Notes

2026-05-23: sera/gateway/platforms/discord.py — parse_discord() unifies 4 surfaces (DM channel.type=1, channel type=0, thread types 10/11/12, slash interaction.type=2) into InboundEvent with metadata.surface tag. _slash_text() handles 0/1/multi-option commands. Bot self-messages + non-DEFAULT types filtered. {"d": …} envelope unwrap for gateway-shape payloads. DiscordSender: send_channel_message → /channels/{id}/messages with optional message_reference; respond_interaction → /interactions/{id}/{token}/callback type=4; reply_hook routes by surface (slash → interaction, dm/thread/channel → message). Auth header `Bot <token>`. DiscordSessionStore: unified per-user (NOT per-surface) — alice/slash + alice/dm + alice/thread share one session_id. last_surface tracked for diagnostics. 24h TTL, injectable clock. Verification: 3-event chain (slash, dm, thread) all hit same session, sender.reply_hook auto-picks correct endpoint each time. 29 tests, 1102 total.
