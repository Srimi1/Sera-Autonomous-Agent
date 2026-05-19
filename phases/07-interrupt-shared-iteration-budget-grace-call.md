# P-07 — Interrupt + shared iteration budget + grace call

## Status

pending.

## Outclass claim

**Shared iteration budget across parent + (future) subagents**, with a 1-call **grace** at exhaustion to summarize cleanly. Hermes per-agent only; nobody ships grace.

## Goal

Ctrl+C returns control fast. Runaway loops cap. Final message never gets truncated by budget.

## Deliverables

- `sera/agent/budget.py` — `IterationBudget` with `remaining`, `consume()`, `grace_used`.
  - `sera/agent/interrupt.py` — per-task cancellation flag; checked after every iteration and after every tool result.
  - Wire into `run_turn`: pass budget down; on `remaining == 0` → one grace call with a system note "summarize and exit"; second exhaustion → `MaxIterations`.

## Files touched

new `sera/agent/budget.py`, `sera/agent/interrupt.py`; edit `sera/agent/loop.py`, `sera/cli/main.py`.

## Verification

```bash
  pytest -q tests/test_budget.py tests/test_interrupt.py
  # manual: in sera chat, send a long task, Ctrl+C; control returns < 200ms
  ```

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
