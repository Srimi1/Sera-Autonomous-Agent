# P-32 — Anonymous peer ranking

## Status

done.

## Outclass claim

**Strict ranking parser tolerant to commentary.** Rejects malformed gracefully.

## Goal

Each model ranks the others; `FINAL RANKING:\n1. C\n2. A\n3. B` parsed reliably.

## Files

`sera/council/rank.py`.

## Verification

test set of 20 ranking outputs all parse or reject correctly.

## Dependencies

P-31.


## Notes

2026-05-22. `sera/council/rank.py` shipped. Three-strategy parser: `numbered_full` (llm-council compat) → `numbered_bare` → `bare_sequence`. Tolerates commentary between items, inline annotations, bold markers, lowercase labels, 5 numbering styles, case-insensitive header. Returns typed `RankingResult`; never raises. 20/20 tests pass; full suite 515/515.
