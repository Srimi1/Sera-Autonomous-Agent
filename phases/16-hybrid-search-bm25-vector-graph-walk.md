# P-16 — Hybrid search (BM25 + vector + graph walk)

## Status

pending.

## Outclass claim

**Fused ranking** — RRF across FTS5, vector cosine, and 1-hop graph neighbours. Rivals pick one signal.

## Goal

"The issue Alice mentioned last week" beats vector-only by ≥20% MRR.

## Deliverables

- `sera/memory/search.py` — `hybrid_search(query, k)` doing the fuse.

## Files touched

`sera/memory/search.py`.

## Verification

```bash
  pytest -q tests/test_hybrid_search.py
  # bench: hybrid MRR > vector-only MRR by ≥0.2 on 50-Q golden set
  ```

## Dependencies

P-11, P-13, P-15.


## Notes

_Journal: decisions, blockers, commit refs go here._
