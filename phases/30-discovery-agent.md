# P-30 — Discovery agent

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Proactive discovery.** Hermes curates what exists; Sera invents what's missing. The discovery agent scans session transcripts, detects tool-call patterns exceeding a frequency threshold, and proposes new skills without the user asking. Rivals wait for user intent; Sera surfaces it unprompted.

## Goal

Daily pass over sessions proposes new skills.

## Deliverables

- `sera/curator/discovery.py`:
  - `MIN_PATTERN_FREQUENCY = 3` — minimum tool-call occurrences to trigger LLM analysis.
  - `DiscoveryProposal(trigger, name, description, body_hint, source_session_ids, reasoning, frequency)` — frozen dataclass.
  - `DiscoveryRun(proposals, sessions_scanned, known_triggers, ran_at, error)` — frozen result.
  - `tool_pattern_counts(sessions)` — heuristic: count per-tool calls across sessions without any LLM.
  - `DiscoveryAgent(llm_call)` — skips LLM entirely when no pattern reaches threshold; known triggers filtered post-parse.
  - `run_discovery(sessions, known_triggers, llm_call)` — convenience daily-pass entry point.
- `sera/cli/main.py`:
  - `sera curator discover [--curator-db PATH] [--dry-run] [--limit N]` — scans curator log for repeated tool_hint patterns, surfaces hot patterns above threshold.

## Files touched

new `sera/curator/discovery.py`; edit `sera/cli/main.py` (1 new subcommand); new `tests/test_discovery.py` (14 tests).

## Verification

```bash
pytest -q tests/test_discovery.py       # 14 passed
pytest -q                                # 480 passed total (was 466 + 14 new)
python -m pyflakes sera/                 # 0 warnings
```

Phase verification clause: `test_five_days_synthetic_usage_yields_proposal` — 5 sessions each calling `web_search` 3× → `DiscoveryRun.proposals` has ≥1 entry.

## Dependencies

P-23, P-25.

## Notes

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `tool_pattern_counts` heuristic — sums tool calls across sessions, no LLM, no threshold logic.
2. RED→GREEN: `DiscoveryAgent.run()` with injected LLM stub — proposals returned, known triggers filtered, LLM skipped when no hot patterns, LLM error → empty run.
3. RED→GREEN: `test_five_days_synthetic_usage_yields_proposal` (verification clause), `run_discovery` convenience wrapper, `DiscoveryProposal` field assertions.
4. RED→GREEN: `sera curator discover` CLI — no sessions → informative message; seeded reports → table of patterns.

**Design decisions (2026-05-21):**

- **Heuristic gate before LLM.** `tool_pattern_counts` is free. LLM only fires when ≥`MIN_PATTERN_FREQUENCY` tool calls exist. Most sessions produce nothing interesting — the gate keeps costs near zero on idle days.
- **Known-trigger filter in parser, not prompt.** The prompt includes known triggers for context, but the parser enforces the filter — LLM hallucination of existing triggers gets dropped silently rather than noisily.
- **`%s` not `.format()` in prompt template.** The template contains JSON braces which `.format()` chokes on. `%s` substitution sidesteps the issue without escaping all braces.
- **`run_discovery` noop LLM default.** With `llm_call=None`, a noop LLM is wired in so the heuristic path still runs. Callers that don't have LLM access get frequency data without proposals — useful for cost-zero environments.
- **CLI scans curator store, not raw sessions.** The daily `discover` command reads the curator's existing store (which already has session summaries) rather than re-parsing raw session files. Single source of truth; zero extra I/O on the hot path.
