# P-38 — Provider fallback chain + FailoverReason

## Status

pending.

## Outclass claim

**Typed reasons** — `RateLimit, Quota, 5xx, Timeout, AuthExpired` — logged + dashboarded.

## Goal

429 → rotate to fallback transparently.

## Files

`sera/llm/failover.py`.

## Verification

simulated 429 → fallback path observed in trace.

## Dependencies

P-37.


## Notes

_Journal: decisions, blockers, commit refs go here._
