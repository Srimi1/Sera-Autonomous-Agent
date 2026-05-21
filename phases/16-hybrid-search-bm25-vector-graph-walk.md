# P-16 — Hybrid search (BM25 + vector + graph walk)

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Fused ranking** — RRF across FTS5, vector cosine, and 1-hop graph neighbours. Rivals pick one signal.

## Goal

"The issue Alice mentioned last week" beats vector-only by ≥20% MRR.

## Deliverables

- `sera/memory/tree.py`:
  - `chunks_fts` FTS5 virtual table with external content (`content='chunks' content_rowid='id'`) over `content` + `summary`. Triggers `chunks_ai` (after insert), `chunks_ad` (after delete), and `chunks_au` (after update) keep the index in sync with `update_chunk` + `delete_chunk` for free.
  - Idempotent FTS migration: legacy upgrade (just-added `extracted_at`) → unconditional `rebuild`; non-legacy + any chunks unindexed → rebuild. The COUNT(*) over chunks_fts is unreliable (FTS5 internal rows inflate it), so the gate uses `SELECT id FROM chunks WHERE id NOT IN (SELECT rowid FROM chunks_fts)`.
  - `idx_chunks_extracted_at` index creation moved into the migration block so legacy DBs without the column don't fail the static schema script.
- `sera/memory/search.py`:
  - `HybridWeights` (bm25=1.0, vector=1.0, graph=0.5 defaults), `HybridHit` (chunk_id, score, content, confidence, sources tuple).
  - `bm25_rank(tree, query, limit)` — `_escape_fts5` tokenises with `\w+` and ORs the terms inside double-quotes so reserved operators in user input never crash the query.
  - `vector_rank(tree, embedding, limit)` — thin wrapper over `tree.search`.
  - `graph_neighbours(tree, query, limit)` — case-insensitive entity lookup over candidate tokens + 2/3-grams; collects 1-hop relation `provenance_chunk_id`s, ranks by accumulated edge confidence.
  - `hybrid_search(tree, query, query_embedding=None, k=10, weights=…, k_rrf=60, pool=…)` — Reciprocal Rank Fusion over the three signals; missing signals (no embedding, zero weight) drop cleanly without crashing.
  - `DEFAULT_RRF_K = 60` per Cormack et al.

## Files touched

new `sera/memory/search.py`; edit `sera/memory/tree.py`; new `tests/test_hybrid_search.py` (19 tests).

## Verification

```bash
pytest -q tests/test_hybrid_search.py    # 19 passed
pytest -q                                  # 231 passed total (was 212 + 19 new)
python -m pyflakes sera/                   # 0 warnings
# bench: "the issue Alice mentioned last week" → target chunk ranks #1 in
# hybrid mode; vector-only ranks it lower (test exercises this).
```

## Dependencies

P-11, P-13, P-15.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **RRF is the right fusion.** No per-signal score normalization needed (BM25 returns negative magnitudes, vss returns L2 distance, graph returns confidence sums — incomparable). RRF rank-only fusion: `weight / (k + rank)`. Standard `k=60`. Multi-signal hits stack naturally.
- **External-content FTS5.** The `content='chunks'` form keeps text bytes in `chunks` and the index in `chunks_fts`. Storage savings + single source of truth. Triggers maintain the index on every write — `update_chunk` and `delete_chunk` from P-14 stay code-free.
- **OR-of-quoted-tokens, not phrase matching.** Natural-language queries ("the issue Alice mentioned last week") don't read as phrase searches — strict AND across every word would miss everything. Quoting each token inside double-quotes neutralises reserved operators (`AND`, `OR`, `*`, `:`); joining with explicit `OR` reflects intent.
- **Graph walk is dumb on purpose.** Token + 2-gram + 3-gram candidates, case-insensitive entity lookup. No LLM in the query path — that's the whole point of fast retrieval. The reward: a query containing "Alice" or "ACME" immediately surfaces chunks via the typed-edge graph from P-15, even when literal content terms miss.
- **FTS migration is non-trivial.** Three failed attempts diagnosed in the journal: (1) `COUNT(*) FROM chunks_fts` is unreliable for external-content tables — FTS5 internal rows inflate it; (2) manual `INSERT INTO chunks_fts(rowid, content, summary)` populates the visible row but doesn't tokenise the content (needs proper rebuild); (3) `INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')` is the canonical re-tokeniser. Final gate: legacy upgrade OR "any chunk without an fts entry by rowid".
- **`idx_chunks_extracted_at` lives in migration.** Pre-P-15 DBs don't have the column; running the index creation in the static schema script would error. Moving it into the migration block — same trigger as the ALTER — keeps the upgrade path clean.
- **Pool ≠ k.** Each signal pulls `max(k*3, 30)` candidates so RRF has overlap room. A small pool starves the fusion; an over-large pool wastes work. 30 floor + 3× growth handles the small-corpus and large-corpus cases without a knob.
- **`sources` tuple on every hit.** Tells the caller (and the user, eventually) which signals surfaced a chunk. Makes "the issue Alice mentioned last week" results explainable — vector matched the verbal phrasing, graph caught Alice, BM25 caught "issue".
- **Skeleton doesn't auto-embed the query.** Caller passes `query_embedding`. The future `sera search` CLI will hand the query to the configured `Embedder` once; the search module stays independent of which embedder is wired.
