# P-11 — Memory Tree schema (SQLite + sqlite-vss)

## Status

pending.

## Outclass claim

**Provenance + confidence** as first-class columns on every chunk and edge. OH stores chunks; nobody else stores confidence per chunk.

## Goal

Persistent long-term memory with vector search.

## Deliverables

- `sera/memory/tree.py` — schema for `chunks(id, source, content, summary, confidence, created_at)`, `chunks_vss(embedding(1536))`, `entities(id, name, type, first_seen)`, `relations(src, dst, kind, confidence, provenance_chunk_id)`.
  - sqlite-vss extension load with bundled fallback to numpy cosine if extension fails.

## Files touched

`sera/memory/tree.py`.

## Verification

```bash
  pytest -q tests/test_memory_tree.py
  ```

## Dependencies

P-09.


## Notes

_Journal: decisions, blockers, commit refs go here._
