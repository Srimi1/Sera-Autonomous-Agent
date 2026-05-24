# P-79 — Curator-of-curators

## Status

done.

## Outclass claim

**Throttles runaway skill churn.**

## Files

`sera/curator/meta.py`, `tests/test_metacurator.py` — 14 tests.

## Verification

20 runaway proposals → 10 accepted, 10 dropped, curator throttled (test_runaway_throttled_in_one_cycle). Sliding window expires; partial acceptance; reset.

## Dependencies

P-23.


## Notes

_Journal: decisions, blockers, commit refs go here._
