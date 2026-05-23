# P-36 — Learned router seed (stats table)

## Status

done.

## Outclass claim

**Per-task-kind table** — provider, model, p50 latency, $/turn, success rate. Live dashboard.

## Goal

Foundation for the bandit.

## Files

`sera/llm/router_stats.py`.

## Verification

`sera route stats` prints after 50 turns.

## Dependencies

P-10.


## Notes

2026-05-23: `sera/llm/router_stats.py` — sqlite `router_calls` table at `~/.sera/router_stats.db`. `record_call` hooks into `run_turn` after every LLM stream (provider, model, task_kind chat|tool, latency_ms, tokens, cost_usd, success). `p50_table()` groups by (provider, model, task_kind) → median latency, avg $/turn, success%. `sera route stats` prints cache table + routing table when ≥50 calls recorded. 14 tests, 640 total. Commit: see git log.
