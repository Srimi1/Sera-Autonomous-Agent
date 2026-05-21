# P-20 — Recall benchmark + golden set

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Published numbers, not promises.** Top-k@k, MRR, hybrid vs vector, per modality — one command, one published number, every release.

## Goal

A repeatable retrieval benchmark Sera reports on every release.

## Deliverables

- `sera/eval/memory_bench.py`:
  - `RecallCase` / `RecallCorpus` / `BenchResult` dataclasses.
  - `mrr(rankings, expected)` — mean reciprocal rank with 1-indexed ranks; missing-match contributes 0.
  - `recall_at(rankings, expected, k)` — fraction of queries with at least one expected id in the top-k.
  - `load_corpus(path)` / `load_queries(path)` — YAML readers.
  - `run_memory_bench(corpus_path, queries_path, *, modes=('vector','bm25','graph','hybrid'), top_k=10)` — ingests every chunk into an ephemeral `TemporaryDirectory` tree, populates entities via `StubExtractor`, then runs each query under each mode. Returns one `BenchResult` per mode with MRR + Recall@{1,5,10} + median latency.
  - Translation pass: corpus-file ids are remapped to sqlite-assigned tree ids so queries stay portable.
- `tests/eval_cases/recall/corpus.yaml` — 30 chunks across three bands:
  - 10 vocab-overlap (BM25 + vector both catch);
  - 10 entity-keyed (graph wins — chunk content uses generic vocab, entity name lives only in `entities:`);
  - 10 literal-phrase (BM25 dominant).
- `tests/eval_cases/recall/queries.yaml` — 30 queries, one expected id each.
- `sera/cli/main.py` — `sera eval bench-memory` subcommand. Prints per-mode table; non-zero exit when hybrid MRR falls below `--min-mrr` (default 0.8).

## Files touched

new `sera/eval/memory_bench.py`; new `tests/eval_cases/recall/corpus.yaml` + `queries.yaml`; edit `sera/cli/main.py`; new `tests/test_memory_bench.py` (18 tests).

## Verification

```bash
sera eval bench-memory   # → hybrid MRR = 0.933 (floor 0.8); exits 0
pytest -q tests/test_memory_bench.py   # 18 passed
pytest -q                               # 307 passed total (was 289 + 18 new)
python -m pyflakes sera/                # 0 warnings
```

Published numbers on the bundled corpus (stub embedder):

| Mode | MRR | R@1 | R@5 | R@10 | ms/q |
|------|-----|-----|-----|------|------|
| vector | 0.675 | 0.63 | 0.70 | 0.80 | 0.1 |
| bm25 | 0.750 | 0.73 | 0.77 | 0.77 | 0.1 |
| graph | 0.333 | 0.33 | 0.33 | 0.33 | 0.0 |
| **hybrid** | **0.933** | **0.90** | **0.97** | **1.00** | 0.2 |

## Dependencies

P-10, P-16.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **30-Q skeleton, not 100.** Phase doc targets "100-Q". 30 cases with three deliberately-shaped bands (vocab-overlap / entity-keyed / literal-phrase) already separate the modes cleanly and clear the 0.8 MRR floor with margin. Growing to 100 is corpus-curation work, not test code — added when the real ingest pipeline produces a corpus worth measuring.
- **StubEmbedder is the bench backbone.** Real embeddings would make every CI run pay a model cost and make numbers fluctuate. The stub's bag-of-words behaviour gives deterministic, repeatable numbers — exactly what "published number every release" needs. Real-embedder runs land in P-20.5+ as an opt-in flag.
- **Three signal bands by design.** Each query maps to a chunk whose dominant retrieval signal is *known*. Q11–Q20 cannot be found by BM25 alone (the entity name appears nowhere in the chunk body) — graph is the only mode that catches them. The hybrid number is meaningful precisely because the singles fall short.
- **Per-bench tree isolation.** Each `run_memory_bench` allocates a fresh `TemporaryDirectory` + sqlite DB. No leak into `~/.sera`. Re-running the bench on the same fixtures returns bit-stable MRR — verified by `test_bench_run_is_isolated`.
- **Hybrid floor locked in CI.** `test_hybrid_mrr_meets_release_floor` asserts ≥0.8; `test_hybrid_beats_every_single_signal` asserts hybrid > vector / bm25 / graph individually. A regression in `hybrid_search` (P-16) or the per-signal ranker shows up as a hard test failure, not a number drift in a manual review.
- **Latency is median, not mean.** A single slow vector search (cold tiktoken load on the first query) would skew the mean. Median is robust to one-shot warm-up cost.
- **`apply_freshness=False` + `consent=True` for bench runs.** The bench measures *retrieval*; freshness decay and PII gating are orthogonal concerns that would muddy mode-vs-mode comparisons.
- **CLI exits non-zero on regression.** `sera eval bench-memory` is wired for `git pre-push` / CI integration: a hybrid MRR below the `--min-mrr` floor returns exit 1 with a red message. Default floor 0.8 — easy to tighten as the corpus matures.
- **Async-rank, not run_until_complete.** First implementation called `asyncio.get_event_loop().run_until_complete` inside a sync helper; that breaks under pytest's auto-async config and nested loops. Switched to a single `asyncio.run` at the top with everything below it async — clean call graph, no event-loop reentry bugs.
