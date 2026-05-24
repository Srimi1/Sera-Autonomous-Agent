# P-89 — Crash-only design + persistence audit

## Status

done (shipped 2026-05-24).

## Outclass claim

**Chaos monkey suite ships as first-class eval.** 5 scenarios: WRITE_ABORT, CONN_DROP, CONCURRENT_WRITES, SCHEMA_INJECT, RECOVERY_IDEMPOTENT. Every seed consistent. No rival self-destructs to prove they survive.

## Outclass claim

**Chaos monkey suite.**

## Files

`sera/eval/chaos.py`.

## Verification

kill random subsystems mid-load; data integrity preserved.

## Dependencies

P-09.


## Notes

_Journal: decisions, blockers, commit refs go here._
