# P-81 — Semantic prompt-injection classifier

## Status

pending.

## Outclass claim

**DistilBERT-sized classifier** scoring every tool output + chunk. H regex-only.

## Files

`sera/safety/injection.py`, `models/injection-cls.onnx`.

## Verification

≥95% recall on a 200-sample set; <2% FP.

## Dependencies

P-08.


## Notes

_Journal: decisions, blockers, commit refs go here._
