# P-10 — Eval harness skeleton + telemetry

## Status

pending.

## Outclass claim

**One command — `sera eval run` — is the release gate.** Nobody on the list has a unified eval CLI you can wire into git pre-push.

## Goal

A small golden-conversation set runs through Sera, scores pass/fail, prints per-turn cost, latency, cache-hit ratio, and tool-call counts.

## Deliverables

- `sera/eval/` — `runner.py`, `cases.py`, `scoring.py`.
  - Golden set: 10 cases under `tests/eval_cases/*.yaml` (prompt, expected tool calls or expected substring in output).
  - Telemetry DB at `~/.sera/telemetry.db` — per-turn rows.
  - CLI: `sera eval run`, `sera eval bench`, `sera eval show`.

## Files touched

new `sera/eval/*`, new `tests/eval_cases/*`, edit `sera/cli/main.py`.

## Verification

```bash
  sera eval run         # expect: 10/10 pass against a stub LLM in CI; ≥8/10 against real provider
  sera eval show        # expect: table with latency + cost
  ```

## Dependencies

P-03.

---


## Notes

_Journal: decisions, blockers, commit refs go here._
