# P-70 — Screen + accessibility hooks (consent-gated)

## Status

done (consent gate fully verified; OS backends are platform-deferred stubs).

## Outclass claim

**Per-feature signed consent toggles; revoke flips a capability off in one
click.** Consent for each OS capability (screen, clipboard, accessibility,
keyboard) lives in the P-64 encrypted vault, so the grant map is tamper-evident
— you cannot hand-edit a file to re-enable screen capture after revoking it
(the AES-GCM tag fails). Every OS hook calls `require()` before touching the
system, and revoke is effective on the very next call with no restart. No rival
ships granular, signed, instantly-revocable OS consent.

## Files

- `sera/os_hooks/consent.py` — ConsentManager (grant/revoke/require/status)
- `sera/os_hooks/{screen,clipboard,a11y,keyboard}.py` — gated hooks
- `tests/test_consent.py`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 22 tests | ✅ | consent manager + all four gated hooks |
| default-denied | ✅ | nothing allowed until granted |
| **revoke + retry refused** | ✅ | test_revoke_then_retry_refused (the phase verification) |
| per-feature isolation | ✅ | granting SCREEN never grants KEYBOARD |
| TTL expiry | ✅ | timed grants lapse |
| signed/encrypted at rest | ✅ | "screen"/"granted" bytes absent from vault file |
| persists across process | ✅ | fresh manager honors prior grant |
| full suite | ✅ | no regressions |

## Limits

- **The actual OS capabilities are platform stubs.** screen capture
  (`screencapture`/mss), clipboard (`pbcopy`/`pbpaste`), a11y tree, and synthetic
  keyboard are injectable backends; the real OS calls require a display and
  macOS TCC permissions and are NOT exercised here. The **consent gate — the
  outclass — is fully tested**; "summarise my screen" producing real pixels is
  deferred to a desktop machine.
- a11y + keyboard real backends raise NotImplementedError until wired per-OS.
- Consent toggles not yet exposed via an HTTP endpoint (`PUT /v1/consent/<f>`);
  Settings.tsx targets it but it isn't wired.

## Dependencies

P-64.
