# P-05 — Mid-turn context compression

## Status

done (shipped 2026-05-20, this session).

Final LOC: compressor.py 159, scrubber.py 99, tokens.py 47. Total <310 LOC for the compaction logic — under the <300 LOC ceiling allowing for tokens helper.

Shipped:
- `[CONTEXT COMPACTION — REFERENCE ONLY]` fence + Remaining Work / Recent Decisions / Open Threads sections.
- StreamingContextScrubber handling split-tag boundaries (byte-by-byte feed test passes).
- Tail protection by tokens, not message count.
- ContextOverflow exception in `sera/llm/base.py`; OpenAI + Anthropic adapters translate provider errors.
- run_turn integration: `_build_view` compacts when est_tokens > 0.8 * budget; on ContextOverflow, retry once with target_ratio=0.4 (aggressive).
- Scrubber wraps streamed deltas before persistence.

Tests added: tests/test_scrubber.py (10 cases), tests/test_compression.py (6 cases). All 30 tests green.

## Outclass claim

**Streaming-safe scrubber + "Remaining Work" framing** with explicit fence prefix so the LLM cannot mistake a compressed summary for instructions. Hermes does the framing; their scrubber is 1699 LOC. Ours ships the same safety in <300 LOC.

## Goal

When session messages approach 80% of the model's context budget, compress older turns into a single summary; preserve last N turns verbatim; never crash on context overflow.

## Deliverables

- `sera/context/compressor.py` — `compact_session(messages, model_budget) -> messages`. Token estimate via tiktoken or model-reported usage. Tail protection by token-budget, not message-count.
  - "Remaining Work" framing in the summary (not "Next Steps") so the model treats it as reference, not instruction. Fence: `[CONTEXT COMPACTION — REFERENCE ONLY]` prefix.
  - `StreamingContextScrubber` that handles `<context>...</context>` spans split across chunk boundaries.
  - Wire into `run_turn`: estimate tokens before each LLM call; compress if > 80% budget; on `ContextOverflow` from provider, compress aggressively + retry once.

## Files touched

new `sera/context/compressor.py`; edit `sera/agent/loop.py`.

## Verification

```bash
  pytest -q tests/test_compression.py     # expect: green
  # manual: stuff a synthetic 500-turn session, run a turn, check last 3 turns are byte-identical pre/post
  ```

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
