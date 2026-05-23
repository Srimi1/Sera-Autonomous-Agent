# P-39 — Cost ceilings (per-day / -session / -skill)

## Status

done.

## Outclass claim

**Hard caps + soft warnings** as first-class config.

## Goal

Bills never surprise.

## Files

`sera/llm/budget.py`.

## Verification

$X soft cap triggers UI banner; hard cap refuses turn.

## Dependencies

P-36.


## Notes

2026-05-23: `sera/llm/budget.py` — BudgetConfig (session/day/skill limits from config), BudgetStatus (OK/SoftWarning/HardBlock), BudgetCheck dataclass, BudgetEnforcer (in-memory session+skill accumulation, DB query for day spend via cost_since). router_stats.cost_since() added. run_turn gains cost_enforcer param: HardBlock raises BudgetExceeded before LLM call; add() called after. _repl shows yellow banner on SoftWarning, red block message on BudgetExceeded. DEFAULT_CONFIG budget section added. 25 tests, 716 total.
