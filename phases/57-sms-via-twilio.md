# P-57 — SMS via Twilio

## Status

done.

## Outclass claim

Foundation adapter — not the headline. Two non-naive additions: (1) inbound
webhooks validated against Twilio's X-Twilio-Signature HMAC so spoofed
messages can't drive the agent; (2) outbound replies measured in real SMS
segments (GSM-7 extension chars, UCS-2 fallback) so the budget system can
price a reply before sending. A one-line Twilio wrapper ships neither.

## Files

- `sera/gateway/platforms/twilio.py` — validate_signature, sms_segments,
  parse_twilio, TwilioSender, TwilioSessionStore
- `tests/test_twilio_adapter.py` — 57 tests covering all layers

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 57-test file | ✅ | All 57 pass — signature, segments, parser, sender, store, router, E2E |
| Full suite | ✅ | No regressions (1102 → 1159 passing) |
| HMAC spoofing | ✅ | test_tampered_body_rejected, test_wrong_url_rejected |
| GSM-7 vs UCS-2 | ✅ | Extension chars (€,{,}) count double; non-GSM forces UCS-2 path |
| 24h continuity | ✅ | test_within_ttl_reuses_session, test_past_ttl_creates_new_session |
| E2E 3-message arc | ✅ | parse → dispatch → send, 23h preserved, 50h reset |

## Dependencies

P-51.

## Notes

Implementation was pre-built; this phase wrote the test suite and closed the
phase. Real Twilio API calls not exercised — all sender tests use `_poster`
injection. Signature verification is the anti-spoofing gate; real deployment
must call `validate_signature` before `parse_twilio` (verified in E2E test).
