# P-31 — Council module (in-process)

## Status

pending.

## Outclass claim

**In-loop council** — llm-council is standalone. Ours fires inside a single agent turn for high-stakes calls.

## Goal

N=3 models answer in parallel, anonymised A/B/C labels.

## Files

`sera/council/runner.py`.

## Verification

`pytest -q tests/test_council.py` — 3 answers, position randomised, no model knows the others' identity.

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
