# P-84 — Audit log w/ tamper-evidence

## Status

pending.

## Outclass claim

**SHA256 chain.** `sera audit verify` flags tampered lines.

## Files

`sera/safety/audit.py`.

## Verification

edit a line → next verify fails on the right line number.

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
