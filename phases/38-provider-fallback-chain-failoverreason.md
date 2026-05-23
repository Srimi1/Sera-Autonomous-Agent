# P-38 — Provider fallback chain + FailoverReason

## Status

done.

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

2026-05-23: `sera/llm/failover.py` — `FailoverReason` enum (RateLimit/Quota/ServerError/Timeout/AuthExpired/Unknown), `classify()` maps HTTP status + message patterns to reason, `FailoverEvent` dataclass with primary/fallback labels + timestamp. `FailoverChain` is LLM-protocol-compatible: wraps a list of adapters, rotates on any non-ContextOverflow error, records typed events. Verification: simulated 429 → fallback path confirmed in chain.events() trace. ContextOverflow not swallowed. 29 tests, 691 total.
