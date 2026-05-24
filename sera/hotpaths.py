"""Hot-path dispatch — Rust extension or pure-Python fallback (P-98).

OUTCLASS: The same API routes to Rust (3×+ faster) when compiled, pure Python
when not. No code changes needed at call sites — `maturin develop --release`
in sera-rust/ is enough to switch.

Build Rust:
    cd sera-rust && maturin develop --release

Verify speedup:
    python -m pytest tests/test_hotpaths.py -v --benchmark
"""
from __future__ import annotations

import math
from typing import Sequence

try:
    import sera_rust as _rust  # type: ignore[import]
    _RUST_AVAILABLE = True
except ImportError:
    _rust = None
    _RUST_AVAILABLE = False


def rust_available() -> bool:
    return _RUST_AVAILABLE


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_bytes: int = 2048) -> list[str]:
    """Split text into chunks ≤ max_bytes, respecting paragraph boundaries."""
    if _RUST_AVAILABLE:
        return _rust.chunk_text(text, max_bytes)
    return _py_chunk_text(text, max_bytes)


def _py_chunk_text(text: str, max_bytes: int) -> list[str]:
    if not text or max_bytes <= 0:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0

    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        pb = len(para.encode("utf-8"))
        if current and current_bytes + 2 + pb > max_bytes:
            chunks.append("\n\n".join(current))
            current = []
            current_bytes = 0
        if pb >= max_bytes:
            # hard-split on words
            word_buf: list[str] = []
            wbytes = 0
            for word in para.split():
                wb = len(word.encode("utf-8"))
                if word_buf and wbytes + 1 + wb > max_bytes:
                    chunks.append(" ".join(word_buf))
                    word_buf = []
                    wbytes = 0
                word_buf.append(word)
                wbytes += (1 if word_buf else 0) + wb
            if word_buf:
                seg = " ".join(word_buf)
                if current and current_bytes + 2 + len(seg.encode()) > max_bytes:
                    chunks.append("\n\n".join(current))
                    current = [seg]
                    current_bytes = len(seg.encode())
                else:
                    current.append(seg)
                    current_bytes += (2 if current else 0) + len(seg.encode())
        else:
            current.append(para)
            current_bytes += (2 if len(current) > 1 else 0) + pb

    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ---------------------------------------------------------------------------
# score_bm25
# ---------------------------------------------------------------------------

def score_bm25(
    query_terms: list[str],
    doc_terms: list[str],
    avg_doc_len: float = 100.0,
) -> float:
    """BM25 score of doc_terms against query_terms. k1=1.5, b=0.75."""
    if _RUST_AVAILABLE:
        return _rust.score_bm25(query_terms, doc_terms, avg_doc_len)
    return _py_score_bm25(query_terms, doc_terms, avg_doc_len)


def _py_score_bm25(
    query_terms: list[str],
    doc_terms: list[str],
    avg_doc_len: float,
) -> float:
    K1, B = 1.5, 0.75
    N = 1_000_000.0
    doc_len = float(len(doc_terms))
    score = 0.0
    for qt in query_terms:
        tf = sum(1 for t in doc_terms if t == qt)
        if tf == 0:
            continue
        idf = math.log((N - 1 + 0.5) / (1 + 0.5))
        tf_norm = (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * doc_len / max(avg_doc_len, 1.0)))
        score += idf * tf_norm
    return score


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if _RUST_AVAILABLE:
        return _rust.cosine_similarity(list(a), list(b))
    return _py_cosine_similarity(a, b)


def _py_cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (mag_a * mag_b)))
