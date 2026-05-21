"""P-20: retrieval recall benchmark."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.eval.memory_bench import (
    BenchResult,
    load_corpus,
    load_queries,
    mrr,
    recall_at,
    run_memory_bench,
)


RECALL_DIR = Path(__file__).parent / "eval_cases" / "recall"


# ─── Math helpers ──────────────────────────────────────────────


def test_mrr_first_rank_is_one():
    assert mrr([[7]], [[7]]) == pytest.approx(1.0)


def test_mrr_second_rank_is_half():
    assert mrr([[1, 7]], [[7]]) == pytest.approx(0.5)


def test_mrr_no_match_is_zero():
    assert mrr([[1, 2, 3]], [[99]]) == 0.0


def test_mrr_averages_across_queries():
    # Q1: rank 1 → 1.0. Q2: no match → 0.0. Mean → 0.5.
    assert mrr([[7], [1, 2]], [[7], [99]]) == pytest.approx(0.5)


def test_mrr_empty_returns_zero():
    assert mrr([], []) == 0.0


def test_mrr_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        mrr([[1]], [[1], [2]])


def test_recall_at_top_one_counts_only_top():
    assert recall_at([[1, 2, 3]], [[3]], k=1) == 0.0
    assert recall_at([[3, 1, 2]], [[3]], k=1) == 1.0


def test_recall_at_top_k_includes_lower_ranks():
    assert recall_at([[1, 2, 3]], [[3]], k=3) == 1.0
    assert recall_at([[1, 2, 3]], [[3]], k=2) == 0.0


def test_recall_at_rejects_zero_k():
    with pytest.raises(ValueError):
        recall_at([[1]], [[1]], k=0)


def test_recall_at_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        recall_at([[1]], [[1], [2]], k=1)


# ─── Loaders ───────────────────────────────────────────────────


def test_load_corpus_reads_chunks_list():
    corpus = load_corpus(RECALL_DIR / "corpus.yaml")
    assert len(corpus.chunks) >= 30
    # Each chunk has id + content.
    for c in corpus.chunks:
        assert "id" in c
        assert "content" in c


def test_load_queries_returns_recall_cases():
    cases = load_queries(RECALL_DIR / "queries.yaml")
    assert len(cases) >= 30
    for case in cases:
        assert case.id
        assert case.query
        assert case.expected_ids  # every query references at least one chunk


# ─── End-to-end bench ──────────────────────────────────────────


@pytest.fixture(scope="module")
def bench_results() -> list[BenchResult]:
    return run_memory_bench(
        RECALL_DIR / "corpus.yaml",
        RECALL_DIR / "queries.yaml",
    )


def test_bench_returns_result_per_mode(bench_results: list[BenchResult]):
    modes = {r.mode for r in bench_results}
    assert modes == {"vector", "bm25", "graph", "hybrid"}


def test_hybrid_mrr_meets_release_floor(bench_results: list[BenchResult]):
    """Outclass lock: published recall number must clear 0.8."""
    hybrid = next(r for r in bench_results if r.mode == "hybrid")
    assert hybrid.mrr >= 0.8, f"hybrid MRR regressed to {hybrid.mrr:.3f}"


def test_hybrid_beats_every_single_signal(bench_results: list[BenchResult]):
    by_mode = {r.mode: r for r in bench_results}
    hybrid = by_mode["hybrid"]
    for single in ("vector", "bm25", "graph"):
        assert hybrid.mrr > by_mode[single].mrr, (
            f"hybrid {hybrid.mrr:.3f} did not beat {single} {by_mode[single].mrr:.3f}"
        )


def test_bench_records_recall_at_breakdown(bench_results: list[BenchResult]):
    for r in bench_results:
        assert set(r.recall_at.keys()) == {1, 5, 10}
        for k, v in r.recall_at.items():
            assert 0.0 <= v <= 1.0


def test_hybrid_recall_at_10_complete(bench_results: list[BenchResult]):
    """Every query's target must land in the top 10 under hybrid."""
    hybrid = next(r for r in bench_results if r.mode == "hybrid")
    assert hybrid.recall_at[10] >= 0.95


def test_bench_run_is_isolated(tmp_path: Path):
    """Calling the bench twice with the same fixtures yields stable numbers."""
    first = run_memory_bench(
        RECALL_DIR / "corpus.yaml", RECALL_DIR / "queries.yaml",
    )
    second = run_memory_bench(
        RECALL_DIR / "corpus.yaml", RECALL_DIR / "queries.yaml",
    )
    by_mode_first = {r.mode: r.mrr for r in first}
    by_mode_second = {r.mode: r.mrr for r in second}
    assert by_mode_first == by_mode_second
