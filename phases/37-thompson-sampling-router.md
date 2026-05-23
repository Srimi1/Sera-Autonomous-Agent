# P-37 — Thompson-sampling router

## Status

done.

## Outclass claim

**Bandit picks model per task kind.** Nobody on the list does this.

## Goal

Cheap wins easy tasks; big wins hard ones.

## Files

`sera/llm/bandit.py`.

## Verification

after 200 synthetic turns, cheap model wins `summarize` slot; big wins `plan` slot.

## Dependencies

P-36.


## Notes

2026-05-23: `sera/llm/bandit.py` — `ThompsonBandit` with Beta(1,1) uniform prior per (profile, task_kind) arm. `pick()` samples all arms, returns argmax. `update(reward)` updates alpha/beta. `reward_signal()` gates on success + latency + cost budgets. `seed_from_stats()` cold-starts from P-36 router_stats. Verification: 200 synthetic turns (50 per arm), cheap wins summarize (≥95/100 picks), big wins plan (≥95/100). 22 tests, 662 total.
