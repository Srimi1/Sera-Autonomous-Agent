# P-19 — Privacy + redaction layer

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Search-with-consent.** PII tagged at ingest; searchable only after an explicit per-query `consent=True`. Rivals scrub at output (or never) — Sera never silently surfaces SSN, card numbers, secret tokens into the agent's working context.

## Goal

Sera never silently surfaces SSN, card number, secret tokens.

## Deliverables

- `sera/memory/privacy.py`:
  - `PIIMatch` dataclass (kind, start, end, text).
  - Regex detectors for `ssn`, `credit_card` (with Luhn check), `email`, `phone`, `ipv4`, plus `anthropic_key`, `openai_key`, `github_pat/oauth/server`, `slack_token`, `aws_access_key`, `aws_session_key`.
  - `detect(text)` returns non-overlapping spans sorted by position; longer / more-specific matches win on tie.
  - `has_pii(text)` short-circuits at the first hit.
  - `pii_kinds(text)` returns deduped tag list (deterministic order).
  - `redact_pii(text, marker="<redacted:{kind}>")` rewrites spans in place.
  - `detect_with_presidio(text)` opt-in: lazy-loads `presidio_analyzer` if importable; remaps Presidio labels to canonical kinds; falls back to regex when the library is absent. `[privacy]` extra (not added to pyproject yet — install manually).
  - `known_kinds()` enumerates the regex backend's tag vocabulary.
- `sera/memory/tree.py`:
  - `chunks.pii_tags TEXT` JSON column. Idempotent migration.
  - `add_chunk` / `add_or_merge_chunk` run the detector on `content` and persist tags.
  - `update_chunk` re-tags on body change (clears tags when PII is removed).
  - `Chunk.pii_tags: tuple[str, ...]` field; `get_chunk` hydrates it.
- `sera/memory/search.py`:
  - `HybridHit` gains `pii_tags` + `redacted: bool`.
  - `hybrid_search(..., consent=False)` is the default. Hits whose chunks have non-empty `pii_tags` get `content` replaced with `"[redacted — pii: kinds; pass consent=True to reveal]"`. Score / sources / pii_tags still visible.
  - `consent=True` returns originals.

## Files touched

new `sera/memory/privacy.py`; edit `sera/memory/tree.py`, `sera/memory/search.py`; new `tests/test_privacy.py` (23 tests).

## Verification

```bash
pytest -q tests/test_privacy.py        # 23 passed
pytest -q                               # 289 passed total (was 266 + 23 new)
python -m pyflakes sera/                # 0 warnings
```

## Dependencies

P-11.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **Tag at ingest, gate at retrieval.** Detecting PII per query would re-run the detector on every read — fine for one chunk, ruinous on a 10k corpus. Tagging at ingest is O(content-size) once; queries become O(1) JSON read.
- **Closed canonical vocabulary.** `known_kinds()` is the tag namespace. Presidio's free-text entity labels are remapped onto canonical Sera kinds (`US_SSN → ssn`, `CREDIT_CARD → credit_card`, etc.) so downstream consumers (CLI display, consent UI, audit logs) don't have to know which backend produced the tag.
- **Luhn check on credit_card.** Bare 13-19-digit runs are too false-positive-prone — phone numbers, tracking ids, invoice numbers all hit. Luhn filters down to actual card-shaped digit strings. Test locks both a valid Visa test card and a Luhn-failing twin.
- **Non-overlapping spans.** Overlap resolution uses first-match-wins on `(start, -length)` sort. A `sk-ant-api01-...` token isn't going to also fire `email` against its content, but the discipline future-proofs against detector additions.
- **Consent default is `False`.** The whole point. Calling `hybrid_search(tree, q)` on a corpus with PII surfaces the chunk *with the kind tags* but not the value. A deliberate `consent=True` is the only way to reveal — easy to grep for in code reviews, easy to log at the call site.
- **Redacted hits still surface.** Suppressing them entirely would leak whether-or-not the chunk exists, which is itself information. The redacted notice carries pii kinds so the user / agent can decide whether revealing is appropriate, without leaking the values.
- **Per-update retagging.** `update_chunk` runs `pii_kinds` on every content change. Tags can shrink to empty when PII is edited out — a true round-trip not just additive accumulation.
- **Presidio is opt-in.** Adding a 200MB+ NLP backend to every install is wrong for a local-first agent. The regex detectors cover the cases that matter for token leakage and the common PII shapes; Presidio is the upgrade path for compliance-grade workflows.
- **Tokens count as PII.** Secret API keys aren't traditionally "PII" but they're the highest-stakes leak vector for an autonomous agent. Treating them as a PII kind reuses one consent gate instead of inventing a parallel "secrets" gate. Same shape, same protections.
- **No vault-side redaction.** `VaultSync` writes chunks to disk verbatim including their bodies — the user is opening files they own in their editor; auto-redacting them would be hostile. The consent gate sits between retrieval and the *agent's* working context, which is where the leak risk lives.
