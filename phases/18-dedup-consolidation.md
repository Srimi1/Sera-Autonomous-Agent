# P-18 — Dedup + consolidation

## Status

pending.

## Outclass claim

**Provenance-preserving merge** — duplicate chunks merge with a chain of source ids so we never lose audit trail.

## Goal

Re-ingestion is a no-op; near-duplicates collapse.

## Deliverables

- Near-duplicate detection (cosine ≥0.95) → merge with combined provenance list.

## Files touched

`sera/memory/tree.py`.

## Verification

```bash
  pytest -q tests/test_dedup.py
  ```

## Dependencies

P-11, P-13.


## Notes

_Journal: decisions, blockers, commit refs go here._
