# P-58 — iMessage (macOS Messages DB + AppleScript)

## Status

done.

## Outclass claim

**Zero relay, zero cloud, zero third-party.** Reads
`~/Library/Messages/chat.db` directly via SQLite read-only URI and sends via
`osascript`. Beeper, BlueBubbles, and Texts.app all require a relay server or
cloud account. Sera needs none — it runs on the same Mac that already has
iMessage configured.

Three specific decisions rivals skip at the adapter layer:

1. **ROWID cursor polling** — no timestamp drift, no duplicate delivery across
   polls. Clock skew cannot cause re-delivery.
2. **Tapback filter** — `associated_message_type != 0` (❤️ 👍 reactions)
   dropped before the event reaches the router. Without this, every tapback
   costs an LLM call.
3. **Nanosecond/second epoch auto-detect** — Big Sur+ stores `date` in
   nanoseconds (> 1e12); older macOS uses seconds. Both parse transparently.

## Files

- `sera/gateway/platforms/imessage.py` — cocoa_to_unix, iMessageReader,
  iMessageSender, iMessageSessionStore, iMessagePoller
- `tests/test_imessage_adapter.py` — 53 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 53-test file | ✅ | All pass: epoch, tapback filter, ROWID cursor, sender, store, router, E2E |
| Full suite | ✅ | No regressions |
| Tapback filter | ✅ | test_outclass_tapback_filtered — 3 rows inserted, 1 delivered |
| ROWID cursor | ✅ | test_outclass_rowid_cursor_no_duplicates — second poll returns [] |
| Nanosecond epoch | ✅ | test_big_sur_epoch_roundtrip — < 0.01s precision |
| Second epoch | ✅ | test_pre_big_sur_roundtrip — < 1s precision |
| E2E 3-message arc | ✅ | poll → dispatch → osascript, 23h preserved, 50h reset |
| Quote injection | ✅ | test_text_with_quotes_sent_safely — \" escaping in AppleScript |

## Limits

**What was NOT tested:**
- Real osascript execution — all send tests use `_runner` injection.
- Full Disk Access not granted — the `?mode=ro` URI will fail with an
  OperationalError; the reader logs a warning and returns [].
- Group chat session routing — sessions are keyed by sender handle; group
  conversations with the same sender from different chats share a session.
- macOS schema variants before Sierra — column set differs; early macOS
  versions lack `associated_message_type`.
- Concurrent writes to chat.db — SQLite WAL allows concurrent reads, but
  the poller is single-threaded by design.

## Dependencies

P-51.
