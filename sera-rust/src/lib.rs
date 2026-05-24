/// Sera Rust hot-paths — P-98.
///
/// Three functions exposed via PyO3:
///   chunk_text(text, max_bytes) -> Vec<String>
///   score_bm25(query_terms, doc_terms) -> f64
///   cosine_similarity(a, b) -> f64
///
/// Build with: maturin develop --release
/// Import:     import sera_rust

use pyo3::prelude::*;

/// Split `text` into chunks of at most `max_bytes` UTF-8 bytes, respecting
/// paragraph boundaries (double newline) when possible.
#[pyfunction]
fn chunk_text(text: &str, max_bytes: usize) -> PyResult<Vec<String>> {
    if text.is_empty() || max_bytes == 0 {
        return Ok(vec![]);
    }
    let mut chunks: Vec<String> = Vec::new();
    let mut current = String::new();

    for para in text.split("\n\n") {
        let para_bytes = para.len();
        if !current.is_empty() && current.len() + 2 + para_bytes > max_bytes {
            chunks.push(current.trim().to_string());
            current = String::new();
        }
        if para_bytes >= max_bytes {
            // Hard-split oversized paragraphs on word boundaries.
            let mut word_buf = String::new();
            for word in para.split_whitespace() {
                if word_buf.len() + word.len() + 1 > max_bytes {
                    if !word_buf.is_empty() {
                        chunks.push(word_buf.trim().to_string());
                        word_buf = String::new();
                    }
                }
                if !word_buf.is_empty() {
                    word_buf.push(' ');
                }
                word_buf.push_str(word);
            }
            if !word_buf.is_empty() {
                current.push_str(&word_buf);
            }
        } else {
            if !current.is_empty() {
                current.push_str("\n\n");
            }
            current.push_str(para);
        }
    }
    if !current.trim().is_empty() {
        chunks.push(current.trim().to_string());
    }
    Ok(chunks)
}

/// BM25 term-frequency score for a single document.
/// k1=1.5, b=0.75 — standard defaults.
#[pyfunction]
fn score_bm25(
    query_terms: Vec<String>,
    doc_terms: Vec<String>,
    avg_doc_len: f64,
) -> PyResult<f64> {
    const K1: f64 = 1.5;
    const B: f64 = 0.75;

    let doc_len = doc_terms.len() as f64;
    let n_docs = 1_000_000.0_f64;  // corpus estimate; real IDF needs corpus stats
    let mut score = 0.0_f64;

    for qt in &query_terms {
        let tf = doc_terms.iter().filter(|t| t.as_str() == qt.as_str()).count() as f64;
        if tf == 0.0 {
            continue;
        }
        let idf = ((n_docs - 1.0 + 0.5) / (1.0 + 0.5)).ln();  // df=1 estimate
        let tf_norm = (tf * (K1 + 1.0))
            / (tf + K1 * (1.0 - B + B * (doc_len / avg_doc_len.max(1.0))));
        score += idf * tf_norm;
    }
    Ok(score)
}

/// Cosine similarity between two equal-length f64 vectors.
#[pyfunction]
fn cosine_similarity(a: Vec<f64>, b: Vec<f64>) -> PyResult<f64> {
    if a.len() != b.len() || a.is_empty() {
        return Ok(0.0);
    }
    let dot: f64 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let mag_a: f64 = a.iter().map(|x| x * x).sum::<f64>().sqrt();
    let mag_b: f64 = b.iter().map(|x| x * x).sum::<f64>().sqrt();
    if mag_a == 0.0 || mag_b == 0.0 {
        return Ok(0.0);
    }
    Ok((dot / (mag_a * mag_b)).clamp(-1.0, 1.0))
}

#[pymodule]
fn sera_rust(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(chunk_text, m)?)?;
    m.add_function(wrap_pyfunction!(score_bm25, m)?)?;
    m.add_function(wrap_pyfunction!(cosine_similarity, m)?)?;
    Ok(())
}
