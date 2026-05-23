# P-55 — WhatsApp (native, not Cloud API)

## Status

done.

## Outclass claim

**Privacy-first** — desktop WhatsApp bridge, not Cloud API.

## Files

`sera/gateway/platforms/whatsapp.py`.

## Verification

phone send → Sera sees + replies.

## Dependencies

P-51, P-70.


## Notes

2026-05-24: `sera/gateway/platforms/whatsapp.py` — `parse_whatsapp()` handles DM (chatId==from), group (@g.us chatId), infers isGroup from JID suffix when flag absent. Skips broadcasts (@broadcast), empty body, missing sender. `WhatsAppSender`: loopback-enforcement guard (raises ValueError for non-127/localhost URLs — privacy guarantee), `send_message()` POSTs {"to", "body"} to bridge /send, `reply_hook()` sends to event.channel_id (group JID for groups, sender JID for DMs). `WhatsAppSessionStore`: 24h per-sender-JID continuity — same user in DM + group chat resolves same Session. `_jid_phone()` helper strips JID suffixes. 34 tests, 1178 total. P-70 dependency deferred (media/screen hooks, text path complete now).
