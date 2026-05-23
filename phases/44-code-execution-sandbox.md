# P-44 — Code execution sandbox

## Status

done.

## Outclass claim

**Tiered sandboxes** — local subprocess → Modal → Daytona, picked by cost ceiling.

## Goal

`python_eval` runs untrusted code safely.

## Files

`sera/tools/impl/python_eval.py`, `sera/sandbox/`.

## Verification

infinite loop killed at 10s; net call refused without grant.

## Dependencies

P-03.


## Notes

2026-05-23: sera/sandbox/ package — SandboxTier(LOCAL/MODAL/DAYTONA), SandboxResult(ok, as_tool_output), Sandbox protocol. LocalSubprocessSandbox: AST scan blocks network imports (requests/socket/urllib/etc.) unless allow_network=True; asyncio.wait_for(timeout) kills process; stripped env (no API keys). picker.pick_sandbox(cost_ceiling_usd) selects cheapest tier; Modal/Daytona fall back to LOCAL if not installed. python_eval tool: code/timeout/allow_network/cost_ceiling_usd params. Verification: infinite loop killed at 2s (timed_out=True); import requests refused without grant. 42 tests, 848 total.
