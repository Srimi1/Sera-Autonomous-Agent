# P-15 — Entity extractor + typed causal-edge graph

## Status

pending.

## Outclass claim

**Typed causal edges with confidence + provenance.** Edge kinds: `mentions, works_at, parent_of, caused, refuted_by, supersedes, similar_to`. Nobody on the list has typed causality.

## Goal

Ask "what caused X" and get the chain.

## Deliverables

- `sera/memory/graph.py` — per-chunk LLM extract → entities + edges (with confidence, provenance).
  - Background pass over existing chunks to backfill.

## Files touched

`sera/memory/graph.py`.

## Verification

```bash
  pytest -q tests/test_graph.py
  # ingest 10 doc corpus; ≥1 `caused` edge produced; "what caused X" returns relevant chunk
  ```

## Dependencies

P-11, P-12, P-13.


## Notes

_Journal: decisions, blockers, commit refs go here._
