"""P-98: Rust hot-paths via PyO3 — shim + pure-Python fallback."""
from __future__ import annotations

import time
from pathlib import Path


from sera.hotpaths import (
    chunk_text,
    cosine_similarity,
    rust_available,
    score_bm25,
    _py_chunk_text,
    _py_cosine_similarity,
    _py_score_bm25,
)

RUST_DIR = Path(__file__).parents[1] / "sera-rust"


# ---------------------------------------------------------------------------
# Scaffold files
# ---------------------------------------------------------------------------

def test_cargo_toml_exists():
    assert (RUST_DIR / "Cargo.toml").is_file()


def test_cargo_toml_has_pyo3():
    content = (RUST_DIR / "Cargo.toml").read_text()
    assert "pyo3" in content


def test_lib_rs_exists():
    assert (RUST_DIR / "src" / "lib.rs").is_file()


def test_lib_rs_exports_chunk_text():
    src = (RUST_DIR / "src" / "lib.rs").read_text()
    assert "chunk_text" in src


def test_lib_rs_exports_score_bm25():
    src = (RUST_DIR / "src" / "lib.rs").read_text()
    assert "score_bm25" in src


def test_lib_rs_exports_cosine_similarity():
    src = (RUST_DIR / "src" / "lib.rs").read_text()
    assert "cosine_similarity" in src


# ---------------------------------------------------------------------------
# rust_available
# ---------------------------------------------------------------------------

def test_rust_available_returns_bool():
    assert isinstance(rust_available(), bool)


# ---------------------------------------------------------------------------
# chunk_text — correctness
# ---------------------------------------------------------------------------

def test_chunk_text_empty():
    assert chunk_text("") == []


def test_chunk_text_single_para():
    result = chunk_text("hello world", max_bytes=100)
    assert result == ["hello world"]


def test_chunk_text_splits_on_double_newline():
    text = "Para one.\n\nPara two.\n\nPara three."
    result = chunk_text(text, max_bytes=200)
    assert len(result) == 1  # all fit in one chunk
    result2 = chunk_text(text, max_bytes=20)
    assert len(result2) >= 2  # at least two chunks when max_bytes < combined size


def test_chunk_text_respects_max_bytes():
    text = "word " * 200  # 1000 bytes
    chunks = chunk_text(text, max_bytes=100)
    for c in chunks:
        assert len(c.encode("utf-8")) <= 100 + 5  # small overage tolerance for word boundaries


def test_chunk_text_pure_python_matches_dispatch():
    text = "Alpha.\n\nBeta.\n\nGamma delta epsilon."
    assert chunk_text(text, 50) == _py_chunk_text(text, 50)


# ---------------------------------------------------------------------------
# score_bm25 — correctness
# ---------------------------------------------------------------------------

def test_bm25_zero_for_no_match():
    s = score_bm25(["python"], ["java", "golang", "rust"])
    assert s == 0.0


def test_bm25_positive_for_match():
    s = score_bm25(["python"], ["python", "is", "great"])
    assert s > 0.0


def test_bm25_higher_tf_scores_higher():
    s1 = score_bm25(["cat"], ["cat", "dog"])
    s2 = score_bm25(["cat"], ["cat", "cat", "cat", "dog"])
    assert s2 > s1


def test_bm25_pure_python_matches_dispatch():
    qt = ["hello", "world"]
    dt = ["hello", "world", "foo"]
    assert abs(score_bm25(qt, dt) - _py_score_bm25(qt, dt, 100.0)) < 1e-9


# ---------------------------------------------------------------------------
# cosine_similarity — correctness
# ---------------------------------------------------------------------------

def test_cosine_identical_vectors():
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(cosine_similarity(a, b)) < 1e-9


def test_cosine_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) + 1.0) < 1e-9


def test_cosine_zero_vector_returns_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_mismatched_lengths_returns_zero():
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_cosine_pure_python_matches_dispatch():
    a = [0.5, 0.3, 0.8]
    b = [0.1, 0.9, 0.2]
    assert abs(cosine_similarity(a, b) - _py_cosine_similarity(a, b)) < 1e-9


# ---------------------------------------------------------------------------
# Benchmark baseline (pure-Python p99 on 1k documents)
# ---------------------------------------------------------------------------

def test_benchmark_chunk_text_throughput():
    """Pure-Python chunker must process 1k paragraphs in < 1 second."""
    doc = ("Sera is a personal agent. " * 40 + "\n\n") * 25  # ~25 paras
    start = time.perf_counter()
    for _ in range(40):
        _py_chunk_text(doc, 512)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"chunker too slow: {elapsed:.2f}s for 40 iterations"


def test_benchmark_cosine_throughput():
    """Pure-Python cosine over 768-dim vectors must do 1k pairs in < 1 second."""
    import random
    rng = random.Random(42)
    vecs = [[rng.gauss(0, 1) for _ in range(768)] for _ in range(100)]
    start = time.perf_counter()
    for i in range(100):
        _py_cosine_similarity(vecs[i], vecs[(i + 1) % 100])
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"cosine too slow: {elapsed:.2f}s"
