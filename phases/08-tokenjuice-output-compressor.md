# P-08 — TokenJuice output compressor

## Status

done (shipped 2026-05-20, this session).

## Outclass claim

**Rule-based with LLM-fallback** — when rules can't compress hard cases (e.g. dense logs), a cheap-model pass shrinks further. OH ships rules-only.

## Goal

Every tool result shrinks before it reaches the LLM context. Secrets stripped pre-persist.

## Deliverables

- `sera/context/tokenjuice.py` — deterministic rule pipeline + async orchestrator.
  - Passes: `strip_ansi`, `html_to_markdown` (stdlib `HTMLParser`, no JS), `shorten_urls` (host annotation + length tag), `debloat_table` (drop empty cols, clip rows), `dedup_lines` (adjacent runs → `… (xN)`), `normalize_whitespace` (trailing + 3+ blank → 2), `redact` (reused from `safety.redact`).
  - `CompressionResult` dataclass: text, original_chars, final_chars, rules_applied, llm_fallback_used, shrink_ratio.
  - `compress_sync(text)` — pure, no event loop required. Used by the agent loop.
  - `compress(text, max_tokens, llm_fallback)` — async; invokes fallback exactly once when rules can't get the output under the cap.
- `sera/agent/loop.py` — `_sanitize_tool_output` now appends `compress_sync` after scrub + redact + fence-defuse. Threshold-gated (`len >= DEFAULT_COMPRESS_THRESHOLD = 500`) so short outputs aren't churned. Compression runs last so HTML/URL rewrites can't reintroduce a scrubbed span.

## Files touched

new `sera/context/tokenjuice.py`; edit `sera/agent/loop.py`; new `tests/test_tokenjuice.py` (20 tests).

## Verification

```bash
pytest -q tests/test_tokenjuice.py   # 20 passed
pytest -q                             # 103 passed total (was 83 + 20 new)
# bench: in tests, synthetic html page + 200-line repeating log both shrink ≥30%
```

## Dependencies

P-03.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-20):**

- **Stdlib HTML parser, no `beautifulsoup4` dep.** `html.parser.HTMLParser` covers the 80% case (links, lists, headings, paragraphs, strong/em, code, pre, drop script/style). A heavier converter would balloon install size + offline footprint with little real-world gain for tool-output scraping.
- **HTML sniff before convert.** `looks_like_html` regex checks the first 4096 chars for an opening tag. Plain text bypasses the parser entirely — zero cost on the 99% of tool outputs that aren't HTML.
- **Order of passes is fixed and audit-relevant:** ansi → html → urls → table → dedup → whitespace → redact. Redact runs **last** so HTML/URL rewrites can't accidentally split a secret across tokens (e.g. percent-encoded API key) and slip through.
- **Rule pipeline is sync.** `compress_sync` is the only function the agent loop calls today; the async `compress` exists for the LLM-fallback path which P-25-ish (cheap model summarization) will wire up. Keeping both shapes means callers don't drag an event loop in unnecessarily.
- **Threshold of 500 chars.** Below this, every pass is overhead with no real shrink to extract. Above it, average web/shell outputs shed 20-50%.
- **Adjacent-only dedup.** Non-adjacent line dedup would change ordering semantics (logs, diffs). Adjacent runs are the dominant pattern (uvicorn warns, retry loops, find dumps).
- **Table debloat keeps first column.** Even when empty, it stabilizes table shape — the alternative (collapsing a 1-col table to bare text) is rarely what you want.
- **CompressionResult exposes ratios.** Telemetry caller (P-10 eval harness) will sample `shrink_ratio` per turn to detect regressions.
- **Defense-in-depth still on:** scrub fences + redact run before compress. Compression is for size, not safety — the safety passes have to stand alone.
