# P-06 — Prompt-cache stability (Anthropic, freeze-at-start)

## Status

pending.

## Outclass claim

**System prompt is hashed and frozen at session start.** Cache_control ephemeral is set on system block + last 3 tool blocks. Kimi proposed it, no rival ships start-locked.

## Goal

Hit prompt cache reliably; cut Anthropic costs ~75% on multi-turn sessions.

## Deliverables

- `sera/llm/cache.py` — `freeze_system_prompt(session)`, `apply_cache_control(messages)` for Anthropic.
  - Persist `system_prompt_hash` column on `sessions` table.
  - On reload of a session, restore the same system prompt verbatim.
  - Telemetry: log `cache_hit_tokens` from Anthropic response usage block; expose via `sera route stats`.

## Files touched

`sera/llm/cache.py`, `sera/llm/adapters/anthropic_adapter.py`, `sera/memory/session.py` (schema migration).

## Verification

```bash
  pytest -q tests/test_prompt_cache.py
  # manual: run 5 turns in one session against Anthropic; usage.cache_read_input_tokens > 0 by turn 2
  ```

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
