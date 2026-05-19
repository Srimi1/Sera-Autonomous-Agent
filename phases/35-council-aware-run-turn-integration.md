# P-35 — Council-aware run_turn integration

## Status

pending.

## Outclass claim

**Per-skill council opt-in.** A skill marked `council: true` triggers ensemble for that single tool call only.

## Goal

No global toggle; council is surgical.

## Files

`sera/agent/loop.py`, `sera/skills/manifest.py`.

## Verification

skill with council:true uses ensemble; without, single model.

## Dependencies

P-34, P-22.


## Notes

_Journal: decisions, blockers, commit refs go here._
