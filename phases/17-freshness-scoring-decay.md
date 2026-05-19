# P-17 — Freshness scoring + decay

## Status

pending.

## Outclass claim

**EWMA decay per chunk** — yesterday's fact outranks last year's contradiction without deleting either.

## Goal

Stale facts demoted; never deleted.

## Deliverables

- `freshness` column on chunks; updated on every read.
  - Retrieval scoring multiplies by freshness.

## Files touched

`sera/memory/tree.py`, `sera/memory/search.py`.

## Verification

```bash
  pytest -q tests/test_freshness.py
  ```

## Dependencies

P-11.


## Notes

_Journal: decisions, blockers, commit refs go here._
