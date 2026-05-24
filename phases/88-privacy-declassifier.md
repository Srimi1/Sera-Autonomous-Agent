# P-88 — Privacy declassifier

## Status

done (shipped 2026-05-24).

## Outclass claim

**Bulk log declassifier with JSON-recursive scrub + before/after diff.** Deep-walks JSONL audit entries, redacts PII at every nesting level, returns unified-style diff of all changed lines. No rival ships a verified bulk scrubber for their own session logs.

## Files

`sera/safety/declassify.py`.

## Verification

pre/post redaction diff on a 1k-line log.

## Dependencies

P-19, P-86.


## Notes

_Journal: decisions, blockers, commit refs go here._
