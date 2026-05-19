# P-03 — LLM adapters + ReAct loop + CLI + safety wiring

## Status

done (shipped 2026-05-19)

## Outclass claim

**Redacted tool-arg echo** — `_shorten_args` masks any key matching `api_key|secret|token|password|bearer|authorization` and hard-truncates to 80 chars. Hermes/OH echo raw args.

## Goal

Talk to OpenAI + Anthropic, stream both, execute a full ReAct turn with an approval gate, and ship a CLI that boots into a REPL.

## Deliverables

- `sera/llm/base.py` — `LLM` Protocol + `StreamChunk`.
  - `sera/llm/adapters/openai_adapter.py` — streaming, native tool calls, tool-call assembly across deltas.
  - `sera/llm/adapters/anthropic_adapter.py` — converts OpenAI-style messages to Anthropic format (tool_use / tool_result blocks), streams `text_delta` + `input_json_delta`.
  - `sera/llm/router.py` — `for_profile(config, profile) -> LLM`.
  - `sera/llm/secrets.py` — env > keyring resolution.
  - `sera/agent/loop.py` — `run_turn(session, user_msg, llm)` with `approval_threshold` parameter; `_effective_permission()` consults runtime classifier for `shell_run`.
  - `sera/safety/approval.py` — `CliApprovalGate` + `AutoApproveGate`.
  - `sera/cli/main.py` — `sera chat / setup / tools / sessions / version`; early API-key probe; `:search` (cross-session) + `:hist` (current session) commands; redacted tool-arg echo via `_shorten_args`.
  - FTS5 escape helper `_escape_fts5` and `current_only` flag on `Session.search`.
  - `Permission.parse(str|int|enum)` for config-string parsing.

## Files touched

all `sera/llm/*`, `sera/agent/loop.py`, `sera/safety/approval.py`, `sera/cli/main.py`, edits to `sera/memory/session.py` and `sera/tools/base.py`.

## Verification

```bash
  pytest -q                    # expect: 14 passed
  sera tools                    # expect: table of 5 tools w/ tiers
  sera sessions                 # expect: empty or your test sessions
  SERA_HOME=/tmp/x sera chat    # expect: red "Missing API key" + exit 1
  ```

## Dependencies

P-01, P-02.


## Notes

_Journal: decisions, blockers, commit refs go here._
