# P-40 — Response distillation cache

## Status

done.

## Outclass claim

**Result-level cache by (prompt-hash, tool-trace-hash).** Nobody ships response distillation.

## Goal

Repeated queries cost cents, not dollars.

## Files

`sera/llm/distill_cache.py`.

## Verification

cache hit rate > 60% on repeated workloads; cost down ≥50% on bench.

## Dependencies

P-37, P-10.

---


## Notes

2026-05-23: `sera/llm/distill_cache.py` — SQLite WAL cache at ~/.sera/distill_cache.db. compute_key(user_msg, tool_msgs) = SHA-256(msg + "\x00" + tool_trace). DistillCache.get() checks TTL expiry, increments hits. put() stores response + cost_usd. evict() by age + max_entries. stats() returns hit_rate, cost_saved_usd. run_turn gets distill_cache param: hit → return cached response immediately, skipping LLM loop; miss → store final_text + _turn_cost after loop. Bench: 10 identical queries → hit_rate=90%>60%, cost_saved=90%>50%. 20 tests, 736 total.
