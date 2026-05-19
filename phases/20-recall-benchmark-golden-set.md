# P-20 — Recall benchmark + golden set

## Status

pending.

## Outclass claim

**Published numbers, not promises.** Top-k@k, MRR, hybrid vs vector, per modality.

## Goal

A repeatable retrieval benchmark Sera reports on every release.

## Deliverables

- `sera/eval/memory_bench.py` — 100-Q recall set; outputs MRR + top-k.

## Files touched

`sera/eval/memory_bench.py`, `tests/eval_cases/recall/*.yaml`.

## Verification

```bash
  sera eval bench memory     # expect: hybrid MRR > 0.8
  ```

## Dependencies

P-10, P-16.

---


## Notes

_Journal: decisions, blockers, commit refs go here._
