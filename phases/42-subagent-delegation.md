# P-42 — Subagent delegation

## Status

done.

## Outclass claim

**Shared iteration budget across parent + subagents** (built on P-07).

## Goal

Parent delegates a task; subagent runs in isolated session.

## Files

`sera/tools/delegate.py`.

## Verification

parent asks "summarize this PDF" → subagent returns string; budget consumed from shared pool.

## Dependencies

P-07, P-03.


## Notes

2026-05-23: `sera/tools/delegate.py` — delegate_task(prompt, llm, budget, workspace, context) creates isolated temp-dir session, runs run_turn with shared IterationBudget, captures output to buffer, returns final_text. make_delegate_tool(llm, budget) wraps it as a Tool (delegate_task) callable by the agent via tool_calls. Budget isolation: subagent consumes from same pool as parent — no unbounded recursion. Verification: "summarize this PDF" → stub returns string, budget.remaining decreases. 16 tests, 775 total.
