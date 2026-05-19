# P-48 — Tool-gen at runtime

## Status

pending.

## Outclass claim

**The big one.** Agent authors a new tool: writes Python → `mypy --strict` → sandbox dry-run → register. Nobody ships this safely.

## Goal

Sera grows its toolbox without code review.

## Files

`sera/tools/genesis.py`, `~/.sera/tools/auto/`.

## Verification

"make me a Hacker News top-stories tool" → working tool in `~/.sera/tools/auto/` and listed in `sera tools` after one turn.

## Dependencies

P-44, P-22.


## Notes

_Journal: decisions, blockers, commit refs go here._
