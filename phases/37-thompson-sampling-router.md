# P-37 — Thompson-sampling router

## Status

pending.

## Outclass claim

**Bandit picks model per task kind.** Nobody on the list does this.

## Goal

Cheap wins easy tasks; big wins hard ones.

## Files

`sera/llm/bandit.py`.

## Verification

after 200 synthetic turns, cheap model wins `summarize` slot; big wins `plan` slot.

## Dependencies

P-36.


## Notes

_Journal: decisions, blockers, commit refs go here._
