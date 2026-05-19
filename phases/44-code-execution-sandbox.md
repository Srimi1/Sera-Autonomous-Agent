# P-44 — Code execution sandbox

## Status

pending.

## Outclass claim

**Tiered sandboxes** — local subprocess → Modal → Daytona, picked by cost ceiling.

## Goal

`python_eval` runs untrusted code safely.

## Files

`sera/tools/impl/python_eval.py`, `sera/sandbox/`.

## Verification

infinite loop killed at 10s; net call refused without grant.

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
