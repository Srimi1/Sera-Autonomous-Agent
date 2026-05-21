# P-15 — Entity extractor + typed causal-edge graph

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Typed causal edges with confidence + provenance.** Edge kinds: `mentions, works_at, parent_of, caused, refuted_by, supersedes, similar_to`. Nobody on the list has typed causality.

## Goal

Ask "what caused X" and get the chain.

## Deliverables

- `sera/memory/tree.py`:
  - `chunks.extracted_at REAL` column + `idx_chunks_extracted_at` index. Idempotent migration runs on every connect via `PRAGMA table_info` introspection.
  - `mark_extracted(chunk_id, when?)` stamps the column.
  - `chunks_pending_extraction(limit=100)` returns IDs of unprocessed chunks in id order so backfill walks chronologically.
- `sera/memory/graph.py`:
  - `EDGE_KINDS` — closed vocabulary; `UnknownEdgeKind` raised at write time if a caller emits anything else.
  - `ExtractedEntity` / `ExtractedEdge` / `ExtractionResult` dataclasses.
  - `Extractor` Protocol — `async extract(text) -> ExtractionResult`.
  - `StubExtractor` — pure-regex canonical verb forms (`X caused Y`, `X works at Y`, etc.). Zero deps, deterministic, used by every test.
  - `LLMExtractor` — JSON-mode prompt with injectable `llm_call`. Provider-agnostic.
  - `parse_llm_extraction(raw)` — accepts JSON string or dict; drops edges with unknown kinds rather than raising; clamps confidence into [0, 1]; rejects non-object inputs.
  - `extract_and_persist(tree, chunk_id, extractor)` — upserts entities, writes relations with `provenance_chunk_id`, stamps `extracted_at`. Validates every edge kind before write.
  - `backfill(tree, extractor, limit)` — runs extraction over `chunks_pending_extraction()`. Per-chunk exceptions log + count toward `chunks_skipped`; chunks stay in the pending queue for retry.
  - `CausalLink` + `causal_chain(tree, entity_name, depth, direction)` — BFS through the `caused` subgraph; `direction ∈ {upstream, downstream}`; cycle-safe via a visited set.

## Files touched

new `sera/memory/graph.py`; edit `sera/memory/tree.py`; new `tests/test_graph.py` (27 tests).

## Verification

```bash
pytest -q tests/test_graph.py        # 27 passed
pytest -q                             # 212 passed total (was 185 + 27 new)
python -m pyflakes sera/              # 0 warnings
# bench: feed 3 chunks "A caused B", "B caused C", "C caused D";
# causal_chain("D", direction="upstream") returns the full A→B→C→D chain.
```

## Dependencies

P-11, P-12, P-13.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **Closed edge-kind vocabulary.** Free-text relations defeat the entire point of "typed causal edges". `EDGE_KINDS` is a 7-element tuple; `UnknownEdgeKind` is raised at persist time so a misbehaving extractor can't silently smear the graph. `parse_llm_extraction` drops unknown kinds rather than raising — one bad LLM hallucination shouldn't waste the rest of the extraction.
- **Stub regex is fragile by design.** It catches canonical verb phrases (`X caused Y`, `X works at Y`) and nothing else. Anything that needs real disambiguation goes through `LLMExtractor`. The stub exists to make the *pipeline* testable without a model — never to be the production extractor.
- **Provenance is required on every write.** `extract_and_persist` always passes `provenance_chunk_id`. `causal_chain` returns those ids on each `CausalLink` so the caller can fetch the source paragraph that justified the link. P-11's per-edge provenance schema pays off here.
- **`extracted_at` is the idempotency key.** Backfill is the standard "every chunk eventually gets processed once" pattern: scan WHERE `extracted_at IS NULL`. The column is indexed so even with 100k chunks the scan stays cheap.
- **Failures stay pending.** A flaky extractor (transient network error, bad LLM JSON) makes `extract_and_persist` raise; `backfill` catches per chunk and *does not* stamp `extracted_at`. The chunk stays in the pending queue for the next run. Tests cover this — `chunks_skipped` ≠ `chunks_processed`.
- **Causal traversal is BFS + visited set.** A naive DFS would chase cycles forever; the visited set short-circuits revisits. Test coverage includes the `A↔B` minimal cycle. Depth is bounded so a runaway graph can't hang.
- **Direction is explicit, not a default.** `upstream` (what caused X) and `downstream` (what did X cause) are distinct questions; defaulting either way is footgun-shaped. Bad strings raise `ValueError` early.
- **Confidence clamping in the parser.** LLMs hallucinate `2.0` confidences (more likely with smaller models). Clamp at parse time so downstream code never has to second-guess the value range.
- **LLM extractor is provider-agnostic.** It takes an `llm_call(prompt) -> awaitable[JSON]`. The wiring to the configured profile (Anthropic / OpenAI) is the agent loop's job — graph stays decoupled from `sera/llm/` so it tests with a 10-line fake.
- **No edge re-dedup yet.** Re-extracting the same chunk would create duplicate relations. For now `extract_and_persist` only runs once per chunk (idempotent via `extracted_at`). When P-16+ enables re-extraction (e.g. updated chunk body), we'll add a "delete prior relations for this provenance" sweep — out of scope here.
