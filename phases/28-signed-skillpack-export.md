# P-28 — Signed `.skillpack` export

## Status

pending.

## Outclass claim

**Signature verification on import.** Hermes ships unsigned `.md` only.

## Goal

Skills travel between machines.

## Files

`sera/skills/pack.py` — zip + manifest + SHA256 + author Ed25519 sig.

## Verification

export → import on a fresh box; sig verifies.

## Dependencies

P-21, P-27.


## Notes

_Journal: decisions, blockers, commit refs go here._
