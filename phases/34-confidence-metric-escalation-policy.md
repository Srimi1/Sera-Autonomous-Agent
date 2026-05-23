# P-34 — Confidence metric + escalation policy

## Status

done.

## Outclass claim

**Kendall-tau across rankings** as a quantitative confidence — escalate to bigger model only when tau < 0.3.

## Goal

Cost-aware council.

## Files

`sera/council/confidence.py`.

## Verification

low-agreement case triggers escalation in test.

## Dependencies

P-33.


## Notes

2026-05-22. `sera/council/confidence.py` shipped. Mean pairwise Kendall-tau, strict tau < 0.3 escalation, typed ConfidenceResult with pairs_evaluated + complete_rankings. Edge cases: 0 or 1 complete rankings → tau=1.0 no escalate. Cyclic disagreement → tau=-1/3 → escalates. 27 tests, full suite 611/611.
