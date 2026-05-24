# P-98 — Cross-language interop (Rust hot-paths via PyO3)

## Status

scaffolded (2026-05-24) — dispatch shim + PyO3 interface complete and tested; **Rust not yet compiled** (`rust_available()==False`), so the speedup is unproven. Promotes to `done` when `maturin develop --release` runs and the benchmark confirms the target.

## Outclass claim

**Same API, automatic acceleration.** `sera.hotpaths` dispatches to compiled Rust (chunk_text, score_bm25, cosine_similarity) when `sera_rust` is present, pure Python otherwise — zero call-site changes. PyO3 interface + Cargo workspace complete; the pure-Python ↔ Rust parity tests pass. The ≥3× p99 drop is the **target, not yet a measurement** — it requires `maturin develop --release` on a Rust toolchain, which has not run in this environment.

## Outclass claim

**Chunker + FTS5 ranker + vector search** in Rust.

## Files

`sera-rust/`.

## Verification

chunker p99 drops ≥3×.

## Dependencies

P-12, P-16.


## Notes

_Journal: decisions, blockers, commit refs go here._
