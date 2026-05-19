# P-40 — Response distillation cache

## Status

pending.

## Outclass claim

**Result-level cache by (prompt-hash, tool-trace-hash).** Nobody ships response distillation.

## Goal

Repeated queries cost cents, not dollars.

## Files

`sera/llm/distill_cache.py`.

## Verification

cache hit rate > 60% on repeated workloads; cost down ≥50% on bench.

## Dependencies

P-37, P-10.

---


## Notes

_Journal: decisions, blockers, commit refs go here._
