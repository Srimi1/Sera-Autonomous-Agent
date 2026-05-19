# P-25 — Skill replay verification

## Status

pending.

## Outclass claim

**Replay-promoted skills.** A new skill cannot move to `active` until it replays cleanly on a captured trace. Hermes promotes by lifecycle; ours by correctness.

## Goal

Bad skills can't reach users.

## Files

`sera/skills/verify.py`, `tests/skill_replay/*.yaml`.

## Verification

broken skill stays in `candidate`.

## Dependencies

P-24.


## Notes

_Journal: decisions, blockers, commit refs go here._
