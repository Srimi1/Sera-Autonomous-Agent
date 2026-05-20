# P-06 — Prompt-cache stability (Anthropic, freeze-at-start)

## Status

done (shipped 2026-05-20, this session).

## Outclass claim

**System prompt is hashed and frozen at session start.** Cache_control ephemeral is set on system block + last 3 tool blocks. Kimi proposed it, no rival ships start-locked.

## Goal

Hit prompt cache reliably; cut Anthropic costs ~75% on multi-turn sessions.

## Deliverables

- `sera/llm/cache.py` — `freeze_system_prompt(session, prompt)`, `apply_cache_control_anthropic(system, messages)`, `parse_anthropic_usage`, `CacheUsage`, `FrozenPromptMismatch`.
  - Persists `system_prompt` + `system_prompt_hash` on `sessions` table.
  - On reload, restores the stored prompt verbatim; tampering raises `FrozenPromptMismatch`.
  - `cache_control: {"type": "ephemeral"}` marker placed on system block + last 3 tool-result blocks (rolling window, 4 total ≤ Anthropic's per-request breakpoint cap).
- `sera/memory/session.py` — additive schema migration (`PRAGMA table_info` introspection, idempotent ALTERs) adds `system_prompt`, `system_prompt_hash`, `cache_read_tokens`, `cache_creation_tokens`, `input_tokens`, `output_tokens`. Adds `record_usage()` and `usage_totals()` accessors.
- `sera/llm/adapters/anthropic_adapter.py` — converts `system: str` into block-list with ephemeral marker, applies tool-result markers, surfaces `usage` (input/output/cache_read/cache_creation) via the final `StreamChunk`.
- `sera/llm/base.py` — `StreamChunk.usage` field added.
- `sera/agent/loop.py` — first turn freezes the prompt; every turn forwards the frozen prompt to the adapter and accumulates usage onto the session row.
- `sera/cli/main.py` — `sera route stats` subcommand reports per-session input/output/cache_read/cache_write and computed hit%.

## Files touched

`sera/llm/cache.py` (new), `sera/llm/adapters/anthropic_adapter.py`, `sera/memory/session.py`, `sera/llm/base.py`, `sera/agent/loop.py`, `sera/cli/main.py`, `tests/test_prompt_cache.py` (new, 14 tests).

## Verification

```bash
pytest -q tests/test_prompt_cache.py    # 14 passed
pytest -q                                # 71 passed total (was 57 + 14 new)
PYTHONPATH=. python -m sera.cli.main route stats   # prints empty table without error
# manual: run 5 turns in one session against Anthropic; usage.cache_read_input_tokens > 0 by turn 2
```

## Dependencies

P-03.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-20):**

- **Window size = 3 tool-result blocks** + 1 system = 4 total breakpoints (Anthropic's per-request cap). Reserving one for the freshly-grown tail keeps the rolling cache useful without forcing a re-creation every turn.
- **Idempotent prompt freeze:** later calls on the same session ignore the passed prompt and return the stored one. Any drift in `SYSTEM_PROMPT` between releases stays invisible to mid-flight sessions, which is the whole point — a single byte change busts the cache.
- **Tamper detection:** stored `system_prompt_hash` compared against `hash_prompt(stored_prompt)` on every freeze. Mismatch → `FrozenPromptMismatch`. Catches direct DB edits or partial-write corruption before the model gets a poisoned prefix.
- **No-mutation contract:** `apply_cache_control_anthropic` deep-copies input messages. The agent loop passes the same view list to compaction + scrubbing — silently mutating it would create heisenbugs.
- **Usage accumulation in `sessions` row** (not a separate `usages` table). Per-session aggregates are the only consumer right now; per-turn granularity isn't needed until P-10 (eval harness).
- **OpenAI adapter unchanged.** Its `usage` block doesn't expose cache_read tokens; only Anthropic ships explicit cache telemetry today. `route stats` will simply show zeros for OpenAI sessions, which is the truthful answer.
