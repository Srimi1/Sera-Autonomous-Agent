# P-27 — Skill versioning + diff

## Status

pending.

## Outclass claim

**Git-tracked skill history.** Every curator edit is a commit.

## Goal

`sera skill log <name>` walks history.

## Files

`sera/skills/git.py` — wraps `git` CLI inside `~/.sera/skills/.git`.

## Verification

`sera skill log file_read_summary` prints commit chain after 3 edits.

## Dependencies

P-22.


## Notes

_Journal: decisions, blockers, commit refs go here._
