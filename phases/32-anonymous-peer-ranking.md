# P-32 — Anonymous peer ranking

## Status

pending.

## Outclass claim

**Strict ranking parser tolerant to commentary.** Rejects malformed gracefully.

## Goal

Each model ranks the others; `FINAL RANKING:\n1. C\n2. A\n3. B` parsed reliably.

## Files

`sera/council/rank.py`.

## Verification

test set of 20 ranking outputs all parse or reject correctly.

## Dependencies

P-31.


## Notes

_Journal: decisions, blockers, commit refs go here._
