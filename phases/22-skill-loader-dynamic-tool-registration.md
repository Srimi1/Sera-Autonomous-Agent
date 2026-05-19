# P-22 — Skill loader + dynamic tool registration

## Status

pending.

## Outclass claim

**Hot reload** — edit a skill, next turn picks it up without restart.

## Goal

Skills become tools at runtime.

## Deliverables

`sera/skills/loader.py` watches skills dir; registers each enabled skill as a Tool.

## Verification

edit a skill → next `sera tools` shows the change.

## Dependencies

P-21.


## Notes

_Journal: decisions, blockers, commit refs go here._
