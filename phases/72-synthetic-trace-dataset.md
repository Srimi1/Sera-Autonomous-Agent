# P-72 — Synthetic trace dataset

## Status

done. **(Second teeth-phase of the autonomy track.)**

## Outclass claim

**mlx-lm / unsloth compatible JSONL, validated before write.**  The
format is the ChatML `messages` list that both frameworks accept without
config shims.  Every export is read back and schema-checked before the
file is considered complete — rivals don't ship the validator.

## Files

- `sera/dream/dataset.py` — `DatasetExporter`, `_validate_record`, `_qa_record`
- `sera/cli/main.py` — `sera export-dataset`
- `tests/test_dataset.py` — 27 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 27 tests | ✅ | validate_record, iter_records, export, validate_file, 100-pair gate |
| **≥100 valid pairs** | ✅ | test_hundred_pairs_valid (7 days × 15 pairs → 105, all validate) |
| ChatML shape | ✅ | user/assistant turns; system role accepted |
| deduplication | ✅ | same (q,a) hash across days → one record |
| empty content skipped | ✅ | blank question or answer filtered before write |
| atomic write | ✅ | .tmp → replace, no partial corpus on disk |
| strip_meta | ✅ | `_meta` key removed on demand for third-party tools |
| read-back validate_file | ✅ | bad JSON + schema failures → line numbers reported |
| full suite | ✅ | no regressions (1503 → 1530) |

## Limits

- **No actual fine-tuning here** — that's P-73 (mlx-lm LoRA). This phase
  produces the corpus; P-73 trains on it.
- **No session-trace format** — only Q-A pairs from DreamEntry are exported.
  Raw session traces (the full message sequence) are a follow-up format if
  P-73 wants supervised instruction fine-tuning beyond Q-A pairs.

## Dependencies

P-71. Feeds P-73 (local LoRA fine-tune).
