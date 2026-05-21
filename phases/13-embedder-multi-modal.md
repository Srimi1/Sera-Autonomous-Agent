# P-13 — Embedder + multi-modal

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Image + text in the same vector space** via vision-caption-then-embed. OH is text-only; H does vision via tools.

## Goal

One vector per chunk regardless of modality.

## Deliverables

- `sera/memory/embedder.py`:
  - `Embedder` Protocol — `dim`, `async embed(text)`, `async embed_batch(texts)`.
  - `StubEmbedder` — deterministic bag-of-words MD5-hash → bucket vector, unit-normalized. Same words → same vector regardless of order. Zero deps, offline-safe, used by every test.
  - `OpenAIEmbedder` — wraps `text-embedding-3-small` (1536-d default; override via `model` + `dim`). Lazy `AsyncOpenAI` client init. Substitutes empty strings with a single space (the API rejects empty `input`).
  - `caption_image_openai(image, *, model="gpt-4o-mini")` — async vision call that returns a short factual caption. Inline base64 data URL via `_image_to_data_url` so we don't need an upload endpoint.
  - `embed_with_image(embedder, image, captioner=None)` — caption + prepend `IMAGE_PREFIX` (`"[image] "`) + embed. Returns `(prefixed_caption, vector)`. Injectable captioner makes the path testable offline.
  - `embed_chunks(embedder, contents)` — convenience batch helper preserving order.

## Files touched

new `sera/memory/embedder.py`; new `tests/test_embedder.py` (15 tests).

## Verification

```bash
pytest -q tests/test_embedder.py        # 15 passed
pytest -q                                # 168 passed total (was 153 + 15 new)
python -m pyflakes sera/                 # 0 warnings
# bench: image query (caption "fluffy cat on windowsill") retrieves the
# text chunk "a fluffy cat resting on a windowsill in soft afternoon light"
# as the top hit, beats unrelated chunk by a margin (test exercises this).
```

## Dependencies

P-11.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **Bag-of-words MD5 stub is the right test backbone.** Real semantic embeddings would either need a model download (slow, no internet in CI) or a paid API (expensive, non-deterministic). The hash stub gives identical vectors for identical text and higher cosine for overlapping vocabularies — sufficient for retrieval-shape tests. Production code never touches it.
- **`[image] ` prefix is a contract, not cosmetic.** A retrieval call wanting only text chunks can `WHERE content NOT LIKE '[image]%'`; a re-ranker can boost or down-weight image-derived hits. Tying the marker to a module-level constant means there's one place to change the convention.
- **Captioner is injectable.** `embed_with_image` accepts `captioner=` so tests pass a deterministic async stub. The default (`caption_image_openai`) is the only place that touches the network — everything else is pure functions of inputs.
- **Vision prompt is single-sentence + concrete.** Empirically, longer captions add noise to embeddings (recipe text, hedge words). The fixed prompt — "describe in one factual sentence, mention objects, setting, visible text" — keeps captions consistent enough that the embedding space stays tight.
- **Image bytes path defaults to `image/png`.** Most clipboard / browser-paste flows are PNG. Adding sniffing (magic-byte detection) is a 5-byte check we can add when a JPEG-via-bytes case actually breaks.
- **Empty input handling.** Embedding `""` returns the zero vector for the stub (well-defined) and a single-space for OpenAI (the API rejects empty strings; the substitute keeps the vector ordering aligned). Tests cover both.
- **No re-embedding on resume.** Embeddings are persisted as `chunks.embedding BLOB` (P-11). Once a chunk is embedded once, recall hits never re-call the model. Multi-modal retrieval is therefore a *one-time* vision cost per asset, not a per-query one — which is exactly what makes the outclass claim hold against Hermes's tool-call-per-image approach.
- **OpenAI SDK error semantics are exposed unmodified.** The embedder doesn't try to normalize provider errors here — the agent loop's adapter layer already owns that contract; the embedder is a different surface.
