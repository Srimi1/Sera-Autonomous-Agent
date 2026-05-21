# P-31 — Council module (in-process)

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**In-loop council** — llm-council is a standalone CLI tool you call from outside your agent. Sera's council fires inside a single agent turn. The user sees one synthesized answer; the ensemble ran invisibly. No external process, no separate CLI invocation.

## Goal

N=3 models answer in parallel, anonymised A/B/C labels.

## Deliverables

- `sera/council/__init__.py` — new package
- `sera/council/runner.py`:
  - `COUNCIL_LABELS = ("A", "B", "C", "D", "E")` — label pool.
  - `CouncilAnswer(label, content, latency_ms, error)` — one member's response; `error` set on failure, model ID never exposed.
  - `CouncilRun(question, answers, label_map, ran_at)` — full result; `label_map` maps `model_id → label`, sealed until synthesis.
  - `run_council(question, models, llm_factory)` — async gather; random label shuffle each run; one member failure produces error-flagged CouncilAnswer, rest proceed.
  - `_call_member` — per-member async wrapper with wall-clock timing.
- `sera/cli/main.py`:
  - `sera council run <question> [--models M1,M2,M3] [--dry-run]` — stub mode shows labels without real API calls.

## Files touched

new `sera/council/__init__.py`, new `sera/council/runner.py`; edit `sera/cli/main.py` (new `council` group + `run` subcommand); new `tests/test_council.py` (15 tests).

## Verification

```bash
pytest -q tests/test_council.py       # 15 passed
pytest -q                              # 495 passed total (was 480 + 15 new)
python -m pyflakes sera/              # 0 warnings
```

Phase verification clause: `test_labels_are_randomised_across_runs` — 20 runs, `model-alpha` gets at least 2 distinct labels. `test_answer_content_does_not_contain_model_id` — model IDs never leak into answer content.

## Dependencies

P-03.

## Notes

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `run_council` returns 3 `CouncilAnswer` instances with content, ran_at set.
2. RED→GREEN: label randomisation across 20 runs; `label_map` covers all models; model IDs not in content; 2-model variant.
3. RED→GREEN: single-model failure → partial run (2 success + 1 error); all-fail → all error-flagged; latency_ms recorded.
4. RED→GREEN: `sera council run --dry-run` prints label table, exit 0.

**Design decisions (2026-05-21):**

- **`llm_factory(model_id) → async_callable`.** Dependency injection keeps the runner test-pure. The factory pattern also lets each model use a different provider client without the runner knowing or caring.
- **Random shuffle per run, not per session.** Randomization at run-time means even if a caller repeatedly asks the same question, model-to-label assignment varies. Prevents ranking bias from label position.
- **`label_map` sealed in `CouncilRun`, not exposed mid-run.** The chairman (P-33) needs model identity for synthesis attribution, but ranking (P-32) must be blind. Sealing it in the result struct enforces the protocol: ranking sees only labels, chairman sees the map after.
- **`BLE001` noqa on member failure catch.** Council is best-effort. One flaky provider should not kill a turn. The error is surfaced in `CouncilAnswer.error` for telemetry without crashing the caller.
- **`COUNCIL_LABELS` tuple, not hardcoded.** Supports 2-model and 4-5 model variants without code changes.
