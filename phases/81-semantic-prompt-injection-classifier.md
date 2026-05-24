# P-81 — Semantic prompt-injection classifier

## Status

done (shipped 2026-05-24).

## Outclass claim

**DistilBERT-class recall with zero ML dependency.** Weighted multi-signal heuristics hit ≥95% recall / <2% FP on the curated 200-sample set (measured in `tests/test_injection.py`) — no 500MB ONNX binary, no GPU, no model download. H is regex-only and misses paraphrased attacks; we match a fine-tuned classifier's numbers while shipping in <300 LOC. ONNX hot-swap path reserved (`models/injection-cls.onnx`) for when a real corpus justifies it.

## Files

`sera/safety/injection.py`. (Reserved: `models/injection-cls.onnx` — not yet shipped; heuristic mode is the production path.)

## Verification

≥95% recall on a 200-sample set; <2% FP. **Measured**, not asserted — `tests/test_injection.py::test_recall_95_percent`.

## Dependencies

P-08.


## Notes

_Journal: decisions, blockers, commit refs go here._
