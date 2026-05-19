# P-08 — TokenJuice output compressor

## Status

pending.

## Outclass claim

**Rule-based with LLM-fallback** — when rules can't compress hard cases (e.g. dense logs), a cheap-model pass shrinks further. OH ships rules-only.

## Goal

Every tool result shrinks before it reaches the LLM context. Secrets stripped pre-persist.

## Deliverables

- `sera/context/tokenjuice.py` — passes: HTML→Markdown, URL shortening, table de-bloat, line dedup, whitespace normalization, secret-pattern redaction.
  - LLM-fallback path for outputs still > N tokens after rules.
  - Wire into `run_turn` after every tool dispatch.

## Files touched

`sera/context/tokenjuice.py`, `sera/agent/loop.py`.

## Verification

```bash
  pytest -q tests/test_tokenjuice.py
  # bench: web_search + shell_run output shrinks ≥30% on the bench set
  ```

## Dependencies

P-03.


## Notes

_Journal: decisions, blockers, commit refs go here._
