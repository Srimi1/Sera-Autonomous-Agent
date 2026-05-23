# P-33 — Chairman synthesis

## Status

done.

## Outclass claim

**Synthesizer is the cheap model.** Cost stays low.

## Goal

Final answer = synthesis of ranked answers.

## Files

`sera/council/chairman.py`.

## Verification

chairman picks consistent winner ≥80% on 50-Q test.

## Dependencies

P-32.


## Notes

2026-05-22. `sera/council/chairman.py` shipped. Borda count aggregation, pluggable cheap synthesis LLM, anonymity preserved (no model names/labels in synthesis prompt), typed `ChairmanResult`, synthesis fallback on LLM error. 69 tests pass including 50-Q parametrized suite (100% pass rate). Full suite 584/584.
