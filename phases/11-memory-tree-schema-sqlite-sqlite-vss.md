# P-11 — Memory Tree schema (SQLite + sqlite-vss)

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Provenance + confidence** as first-class columns on every chunk and edge. OH stores chunks; nobody else stores confidence per chunk.

## Goal

Persistent long-term memory with vector search.

## Deliverables

- `sera/memory/tree.py` — `MemoryTree` class wrapping a SQLite store with:
  - `chunks(id, source, content, summary, confidence, embedding BLOB, created_at)` — confidence is REAL ∈ [0, 1], clamped by `add_chunk`.
  - `chunks_vss` — sqlite-vss `vss0` virtual table over the embedding column, created only when the extension loads cleanly.
  - `entities(id, name UNIQUE, type, first_seen, last_seen)` — `add_entity` upserts by name and bumps `last_seen` so freshness queries (P-17) work without a separate index.
  - `relations(id, src_entity_id, dst_entity_id, kind, confidence, provenance_chunk_id, created_at)` — every edge carries confidence + a back-pointer to the chunk that justifies it.
  - `add_chunk` / `add_entity` / `add_relation` / `get_chunk` / `find_entity` / `relations_for`.
  - `search(query, limit, min_confidence)` — picks sqlite-vss when available, else numpy cosine over the embedding BLOBs. Same `SearchHit` shape either way, with cosine→distance mapping so ordering is identical across backends.
  - Standalone helpers `cosine_similarity`, `euclidean_distance` for callers without a tree handle.
  - WAL + synchronous=NORMAL on connect, mirroring P-09's session-store discipline.
- `pyproject.toml` — `numpy>=1.26.0` added to required deps; `sqlite-vss>=0.1.2` lives under `[project.optional-dependencies] vss`. `pyflakes>=3.0.0` added to `dev` so `/test` finds it without a manual install.
- Pre-existing pyflakes cleanup rolled in:
  - `sera/context/scrubber.py` — drop unused `typing.Iterable`.
  - `sera/context/compressor.py` — drop unused `estimate` from the tokens import.

## Files touched

new `sera/memory/tree.py`; edit `pyproject.toml`, `sera/context/scrubber.py`, `sera/context/compressor.py`; new `tests/test_memory_tree.py` (15 tests).

## Verification

```bash
pytest -q tests/test_memory_tree.py   # 15 passed
pytest -q                              # 137 passed total (was 122 + 15 new)
python -m pyflakes sera/               # 0 warnings
```

## Dependencies

P-09.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **sqlite-vss is optional.** Tier 1 (extension loaded) handles k-NN inside SQL; Tier 2 (numpy fallback) scans every embedded chunk in pure Python with vectorized ops. The fallback is O(N) per query — acceptable up to ~10k chunks, after which the user installs `sera[vss]`. We probe the extension exactly once per process via `_VSS_AVAILABLE` / `_VSS_CHECKED` flags.
- **Embeddings stored as `float32` BLOB.** Half the storage cost of float64, identical recall in practice for cosine-distance retrieval. `_embedding_to_blob` / `_blob_to_embedding` round-trip via `numpy.ndarray.tobytes()` so endianness is whatever numpy picks (little on every supported host).
- **Cosine→distance mapping in numpy path.** sqlite-vss returns L2 distance (smaller = closer). We map cosine similarity → `1 - cosine` so the SearchHit ordering is consistent. Both paths sort ascending by `distance`.
- **Confidence is a hard contract, not a vibe.** Every `add_chunk` and `add_relation` clamps `[0, 1]` and raises `ValueError` otherwise. `min_confidence` filter on `search` keeps weak chunks out of recall results without re-ranking after the fact.
- **Provenance is required for relations.** A `provenance_chunk_id` column on `relations` lets every edge trace back to the text that justified it. `relations_for(name)` returns hits with their chunk-ids; the caller can `get_chunk(provenance_chunk_id)` to render the source paragraph. P-15 (entity-extractor) will populate this automatically.
- **Entity upsert by name.** Two `add_entity(name=…)` calls with the same name return the same id and bump `last_seen`. P-15 / P-50+ extraction pipelines can call this freely on every mention without dedup logic.
- **MemoryTree owns its connection.** Like Session, the tree opens once and reuses. `close()` + `with tree.session() as t:` mirrors the session contract.
- **Skeleton does NOT auto-embed.** `add_chunk` takes a pre-computed embedding (or None). P-13 (embedder) hooks the real model in; the tree stays embedding-source agnostic so swapping providers is a one-line callsite change.
- **Pyflakes cleanup is in this phase deliberately.** Both warnings were in files this phase imports (`scrubber` via loop sanitizer, `compressor` via the same loop). Rolling them in keeps the diff focused on "memory/context layer" and avoids a one-line cleanup PR.
