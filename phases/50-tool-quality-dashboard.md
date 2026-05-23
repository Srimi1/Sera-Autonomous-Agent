# P-50 — Tool quality dashboard

## Status

done.

## Outclass claim

**Per-tool usage / success / latency / $/call** in `sera tools --stats`.

## Goal

Drift visible.

## Files

`sera/tools/stats.py`, `sera/cli/main.py`.

## Verification

real numbers after the bench suite.

## Dependencies

P-37, P-49.

---


## Notes

2026-05-23: sera/tools/stats.py — tool_calls table at ~/.sera/tool_stats.db (tool_name, latency_ms, success, error_msg, cost_usd, recorded_at). record_tool_call() called by dispatcher.execute() in finally block — captures both success and failure paths with t0=time.monotonic() timing. ToolStatRow: tool_name, n_calls, n_ok, n_fail, success_pct, p50_ms, avg_latency_ms, avg_cost_usd, last_used_at. tool_stats() groups by name and aggregates; stats_for(name) lookup; clear_stats() reset. sera/cli/main.py: `sera tools --stats` flag prints second table with calls/ok%/p50/avg/cost/call columns. Verification: 30-call bench across alpha (10 ok), beta (10 fail), gamma (10 ok) → tool_stats() returns 3 rows with correct success_pct (100/0/100), n_calls=10 each, total=30. Drift detector visible. 18 tests, 999 total.
