# P-80 — Hill-climb regression suite

## Status

done.

## Outclass claim

**No LoRA promotes without beating last night.**

## Files

`sera/eval/regress.py`, `tests/test_regress.py` — 20 tests.

## Verification

Bad LoRA (score ≤ baseline) never promotes; good LoRA (score > baseline) always does; first night promotes unconditionally; baseline = last night not historical peak.

## Dependencies

P-73, P-10.

---


## Notes

_Journal: decisions, blockers, commit refs go here._
