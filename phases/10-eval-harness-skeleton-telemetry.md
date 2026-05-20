# P-10 — Eval harness skeleton + telemetry

## Status

done (shipped 2026-05-20, this session).

## Outclass claim

**One command — `sera eval run` — is the release gate.** Nobody on the list has a unified eval CLI you can wire into git pre-push.

## Goal

A small golden-conversation set runs through Sera, scores pass/fail, prints per-turn cost, latency, cache-hit ratio, and tool-call counts.

## Deliverables

- `sera/eval/`:
  - `cases.py` — `EvalCase`, `ExpectedOutcome`, `ScriptStep` dataclasses; `load_case` / `load_cases` yaml readers.
  - `stub_llm.py` — `StubLLM` that replays a case's `script` step by step (text + tool_call_deltas + finish_reason). Deterministic, CI-safe.
  - `scoring.py` — `score(case, session, iterations)` checks `substring`, `tool_calls`, `forbid_tool_calls`, `min_iterations`, `max_iterations`. Returns `ScoreVerdict`.
  - `telemetry.py` — `TelemetryStore` at `~/.sera/telemetry.db` (or override). Schema: `runs` (id, started, finished, profile, n_pass, n_fail) + `results` (per-case latency_ms, tool_calls_count, token totals, passed, reason). Idempotent init.
  - `runner.py` — `run_cases(cases, telemetry, profile)`. Each case gets a fresh `TemporaryDirectory` workspace seeded with `workspace_files`, a fresh `sessions.db`, a fresh `Session`, an `AutoApproveGate(allow=True)`, and the configured `IterationBudget`. Returns `RunReport`.
- `tests/eval_cases/` — 10 golden yaml cases: greet, quick_math, file_read_happy, file_write_happy, shell_ls_safe, memory_store_basic, read_then_write, refuse_destructive, respect_min_tools, iterate_then_finalize.
- `sera/cli/main.py` — `sera eval` group with `run`, `bench`, `show` subcommands. `run` exits non-zero on any failure (CI-ready). `show` prints the recent runs table from telemetry.

## Files touched

new `sera/eval/__init__.py`, `cases.py`, `stub_llm.py`, `scoring.py`, `telemetry.py`, `runner.py`; new `tests/eval_cases/*.yaml` (10 files); new `tests/test_eval.py` (9 tests); edit `sera/cli/main.py`.

## Verification

```bash
python -m sera.cli.main eval run --no-store   # → 10/10 passed (stub)
pytest -q tests/test_eval.py                  # 9 passed
pytest -q                                      # 122 passed total (was 113 + 9 new)
```

## Dependencies

P-03.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-20):**

- **Stub mode is the default for `sera eval run`.** A real-LLM bench mode is a P-10.5-ish follow-up — wiring a config profile through the runner is one knob, but it makes the harness slow + costly + non-deterministic. The skeleton needs to be the kind of thing you put in a pre-push hook, so deterministic stub-driven is the right floor.
- **`ScriptStep.tool_calls` uses the OpenAI shape (`id`, `name`, `arguments`)**, even though the stub is provider-agnostic. The agent loop normalizes both Anthropic + OpenAI deltas into this shape upstream, so cases stay portable.
- **Per-case isolation is workspace + DB + session, all under `TemporaryDirectory`.** No leak into `~/.sera/sessions.db`. Memory-store cases still write to `~/.sera/memory.db` (long-term memory is shared on purpose) — fine for skeleton; if it bites we'll add a per-case memory.db override.
- **Telemetry is opt-out (`--no-store`).** Default is to persist so `sera eval show` always has something to render.
- **CLI exits non-zero on failure.** `sera eval run` returns 1 if any case failed; CI wiring is `git push -o ci.skip || sera eval run`.
- **No real-LLM stub-replacement in P-10.** Phase 10.5 (or whenever) adds `--profile anthropic` that builds the configured LLM and runs cases that only specify `prompt` + `expect` (no `script`). The same scoring layer applies.
- **Default cases-dir resolution.** CLI looks for `./tests/eval_cases` first (dev), then walks up to the package source. Editable installs work. A pip-installed wheel would need cases bundled — explicitly out of scope for the skeleton.
- **The 10 golden cases tag every tool surface** (filesystem, shell, memory) plus two no-tool cases (greet, quick_math) and two multi-step cases (read_then_write, iterate_then_finalize). That spread is what surfaces regressions in the ReAct loop quickly.
