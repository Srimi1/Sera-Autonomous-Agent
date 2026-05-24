# P-77 — Cross-session consolidation

## Status

done.

## Outclass claim

**Contradictions surface to user.**

## Files

`sera/dream/consolidate.py`, `tests/test_consolidate.py` — 12 tests.

## Verification

3 contradictions → 1 reconciliation prompt (test_three_contradictions_one_prompt). LLM soft-fail; malformed items skipped; <2 facts skips LLM.

## Dependencies

P-15.


## Notes

_Journal: decisions, blockers, commit refs go here._
