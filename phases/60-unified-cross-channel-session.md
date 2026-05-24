# P-60 — Unified cross-channel session

## Status

done. **(Epoch 6 capstone.)**

## Outclass claim

Two things no rival ships together:

1. **One session DB across every channel.** The per-platform stores
   (P-52..P-58) keyed sessions by the platform's own user_id, so Telegram-42,
   Slack-U123, and iMessage-+1415 were three separate sessions even for the
   same person. `IdentityStore` adds a layer: `(platform, channel_user_id) →
   identity_id → one shared Session`. Link the handles once; the conversation
   is continuous across all of them. Ask on Telegram, follow up on Slack,
   context preserved.
2. **Privacy-first reply routing (native > cloud).** Each platform carries a
   `PrivacyTier` (NATIVE / SELF_HOSTED / CLOUD). When a person is reachable on
   several channels, `preferred_channel` returns the most-private one —
   iMessage (NATIVE, local-only) beats Telegram (CLOUD) for the outbound reply.

`store.resolver()` is a drop-in Router `session_resolver`, identical interface
to the per-platform stores it supersedes. Auto-create defaults to a fresh
identity per new handle (safe: never merge two people by accident); explicit
`link`/`merge` makes sessions converge.

## Files

- `sera/gateway/identity.py` — PrivacyTier, PLATFORM_PRIVACY, IdentityStore
  (link/unlink/merge, get_or_create_session, resolver, preferred_channel)
- `tests/test_identity.py` — 32 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 32-test file | ✅ | identity mgmt, unification, merge, privacy routing, cross-channel |
| **Cross-channel reference** | ✅ | test_ask_on_telegram_follow_up_on_slack_same_session — TG question + Slack follow-up → ONE session via real Router |
| Three channels, one session | ✅ | TG+Slack+iMessage collapse to one owner session |
| Strangers stay separate | ✅ | unlinked handles never collide |
| Native > cloud routing | ✅ | preferred_channel picks iMessage over Telegram, email over Slack |
| Merge keeps freshest session | ✅ | test_merge_after_use_unifies_sessions |
| 24h TTL preserved | ✅ | reuse within 24h, reset past it (per identity, not per handle) |
| Full suite | ✅ | No regressions |

## Limits

**What was NOT tested / deferred:**
- **Auto-linking by shared verified contact.** Today linking is explicit
  (`link`/`link_all`) or manual `merge`. Auto-detecting that Slack-profile-email
  == Email-adapter-address, or Twilio-number == iMessage-handle, is not wired —
  it would need the adapters to surface verified contact fields. Designed for
  but not implemented; explicit linking is the safe v1.
- **`preferred_channel` is advisory.** It returns the most-private channel but
  the gateway's `on_response` hook is still per-adapter; nothing yet rewrites an
  outbound reply onto the preferred channel automatically. That's a gateway
  wiring step (a future phase), not an identity-layer gap.
- **Concurrent dispatch on one identity.** Two channels firing for the same
  identity simultaneously both resolve the same session row; SQLite serializes
  the writes but interleaved turns on one Session are not tested.
- **Identity DB migration from per-platform stores.** Existing Telegram/Discord
  session DBs are not back-filled into the identity DB; this is additive.

## Dependencies

P-52..P-59. Closes Epoch 6 (Channels).
