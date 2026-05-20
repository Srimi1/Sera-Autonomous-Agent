# P-07 — Interrupt + shared iteration budget + grace call

## Status

done (shipped 2026-05-20, this session).

## Outclass claim

**Shared iteration budget across parent + (future) subagents**, with a 1-call **grace** at exhaustion to summarize cleanly. Hermes per-agent only; nobody ships grace.

## Goal

Ctrl+C returns control fast. Runaway loops cap. Final message never gets truncated by budget.

## Deliverables

- `sera/agent/budget.py` — `IterationBudget` dataclass with `total`, `remaining`, `grace_used`, `consume()`, `can_request_grace()`, `request_grace()`. Raises `MaxIterations` past the grace boundary.
- `sera/agent/interrupt.py` — `InterruptToken` (threadsafe one-shot), `Interrupted` exception, `install_sigint(token)` context manager (routes Ctrl+C to `token.set()`, restores prior handler on exit, second Ctrl+C re-raises `KeyboardInterrupt`).
- `sera/agent/loop.py` — `run_turn` now accepts `budget` and `interrupt`. Per-iteration: `interrupt.check()` → `budget.consume()`. On `MaxIterations` with grace available: refund one iteration, persist `GRACE_NOTICE` as a user message, run the grace turn with `tools=None`, break after it. Post-tool `interrupt.check()` lets Ctrl+C cancel between long tools.
- `sera/cli/main.py` — REPL builds a fresh `IterationBudget` + `InterruptToken` per turn, wraps `run_turn` in `install_sigint(token)`. `Interrupted` → yellow `[interrupted]` and continue REPL. `KeyboardInterrupt` (double Ctrl+C) → exit REPL cleanly.

## Files touched

new `sera/agent/budget.py`, `sera/agent/interrupt.py`; edit `sera/agent/loop.py`, `sera/cli/main.py`; new `tests/test_budget.py` (7 tests), `tests/test_interrupt.py` (5 tests).

## Verification

```bash
pytest -q tests/test_budget.py tests/test_interrupt.py   # 12 passed
pytest -q                                                 # 83 passed total (was 71 + 12 new)
# manual: in sera chat, send a long task, Ctrl+C; control returns < 200ms
```

## Dependencies

P-03.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-20):**

- **Budget is mutable + shared by reference.** Subagent spawning in later phases (P-31 council, P-50+ delegation) will pass the same `IterationBudget` instance to children. One counter, no negotiation.
- **Grace is one iteration, tools disabled.** During grace mode the loop passes `tools=None` so the model physically cannot tool-call. If it still emits a tool_use block (rare jailbreak attempt or model bug), the loop breaks immediately after persisting the assistant text — no execution, no extra round.
- **GRACE_NOTICE persisted as a user message.** It's part of true session history — multi-turn replay needs to know why the assistant suddenly summarized. Persisting as `role=system` would collide with the frozen system prompt (P-06).
- **Interrupt checked at iteration boundaries + post-tool, never mid-stream.** Cancelling mid-delta would leave session.messages out of sync with assistant output. The 200ms target falls out of post-tool checks (tools dominate latency).
- **Tools cannot be killed.** A shell_run that's writing files or a file_write mid-flush stays running to completion before the loop honors the cancel. Killing mid-tool would leave half-written files. The accepted tradeoff: the user waits for the current tool, never longer.
- **Double Ctrl+C raises `KeyboardInterrupt`.** First Ctrl+C cancels the turn; second exits the REPL. Anything in-flight gets the standard Python interpreter cleanup path.
- **Back-compat with `max_iterations` int.** Callers passing `max_iterations=N` and no `budget` still work — the loop builds a budget from N. Existing tests didn't change.
