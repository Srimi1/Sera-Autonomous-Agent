# P-12 — Semantic chunker

## Status

pending.

## Outclass claim

**Heading-aware metadata** — each chunk keeps its heading chain so search results read like Wikipedia citations.

## Goal

Split markdown / text into ≤3k-token chunks that respect document structure.

## Deliverables

- `sera/memory/chunker.py` — markdown AST split by heading > paragraph > line; 10% overlap; preserved heading path in chunk metadata.

## Files touched

`sera/memory/chunker.py`.

## Verification

```bash
  pytest -q tests/test_chunker.py    # round-trip a 50-page MD, all headings preserved
  ```

## Dependencies

P-11.


## Notes

_Journal: decisions, blockers, commit refs go here._
