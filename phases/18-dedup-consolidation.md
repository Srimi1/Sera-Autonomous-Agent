# P-18 — Dedup + consolidation

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Provenance-preserving merge** — duplicate chunks merge with a chain of source ids so we never lose audit trail. Rivals dedupe by hash + drop; Sera keeps every merged source label as a typed JSON entry on the canonical row.

## Goal

Re-ingestion is a no-op; near-duplicates collapse.

## Deliverables

- `sera/memory/tree.py`:
  - `chunks.merged_into INTEGER REFERENCES chunks(id)` — when non-null, this row was deduped into another. Retrieval skips these rows.
  - `chunks.merged_from TEXT` — JSON array of `{source, similarity, at}` entries recording every duplicate that merged in. Audit chain.
  - `DEFAULT_DEDUP_THRESHOLD = 0.95` — cosine similarity floor for "near-duplicate".
  - `find_near_duplicate(embedding, threshold)` — wraps `tree.search(limit=1)`; returns `(canonical_id, similarity)` only when the top hit is above threshold. The underlying vector search already skips merged rows, so the result is always canonical.
  - `add_or_merge_chunk(...)` — drop-in replacement for `add_chunk` that does dedup-aware insertion. Returns `(chunk_id, merged_bool)`. On merge: append source to `merged_from`, bump `confidence = max(existing, new)`, touch freshness.
  - `resolve_canonical(id)` — follows `merged_into` pointers, cycle-safe via visited set.
  - `merged_from_for(id)` — JSON decode helper.
  - Idempotent ALTER migration for legacy DBs.
  - `_search_vss` and `_search_numpy` now filter `WHERE merged_into IS NULL`.
- `sera/memory/search.py`:
  - `bm25_rank` joins with `c.merged_into IS NULL` filter.
  - `graph_neighbours` resolves `provenance_chunk_id` through `resolve_canonical` so graph hits surface the live canonical even when the originally-cited chunk got merged.

## Files touched

`sera/memory/tree.py`, `sera/memory/search.py`; new `tests/test_dedup.py` (16 tests).

## Verification

```bash
pytest -q tests/test_dedup.py         # 16 passed
pytest -q                              # 266 passed total (was 250 + 16 new)
python -m pyflakes sera/               # 0 warnings
```

## Dependencies

P-11, P-13.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **Soft delete, not hard delete.** A merged duplicate keeps its row, content, and embedding; it just sets `merged_into` and disappears from search results. Trade: ~50 bytes per duplicate row for a permanent audit trail. Worth it — the whole outclass claim hinges on not losing the source ids.
- **Two-column design (`merged_into` + `merged_from`).** `merged_into` is the *redirect* (one int, easy to follow). `merged_from` is the *audit chain* (JSON list on the canonical). They're orthogonal — a chunk can have one without the other and still be valid.
- **JSON list, not normalized table.** A separate `merge_events` table would be cleaner schema-wise but slower to fetch and harder to reason about. Per-chunk JSON keeps the audit trail co-located with the canonical row; one read = full history.
- **Confidence on merge is `max`, not `mean`.** A high-confidence re-ingestion of a low-confidence chunk should *promote* the chunk, not split the difference. Mean would punish strongly-vouched-for facts when noisy duplicates arrive.
- **Threshold 0.95 default.** Empirically: distinct paraphrases of the same fact cluster in 0.85-0.92 with the stub embedder; identical re-ingestion lands above 0.97. 0.95 is the safe gap. Callers can tune per-call.
- **No embedding ⇒ no merge.** Without an embedding we can't compute similarity. The conservative call is to always insert; lossy dedup based on string equality would catch obvious cases but miss the more common "paraphrase of yesterday's tool output". The user wants embeddings on by default anyway (P-13).
- **Graph resolution at retrieval, not write time.** When a chunk gets merged, every relation already pointing at it via `provenance_chunk_id` still references the old (now-dead) row. Rewriting those rows would be invasive and prone to broken references on partial failures. Instead, `graph_neighbours` calls `resolve_canonical` on the way out — provenance pointers stay frozen, but live queries always land on the canonical.
- **`resolve_canonical` is cycle-safe.** A manual SQL poison can build an A↔B cycle. The visited set short-circuits — we return *some* node from the cycle rather than looping. Tests cover both clean chain and cycle.
- **Merge touches freshness.** A re-ingestion *is* a recall — bumping `freshness` via `touch_chunk` keeps repeatedly-seen facts sharp without polluting the EWMA math (one touch = one event, no double-counting).
- **Filter at the SQL layer, not in Python.** Filtering `merged_into IS NULL` in the WHERE clause means the index helps and we don't shuttle dead rows through the network. For the numpy fallback, the same WHERE keeps the candidate count down.
