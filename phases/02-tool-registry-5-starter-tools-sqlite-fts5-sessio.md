# P-02 — Tool registry + 5 starter tools + SQLite/FTS5 session store

## Status

done (shipped 2026-05-19)

## Outclass claim

none yet — table stakes.

## Goal

Tool abstraction with permission tiers, auto-discovery, and a session store powered by SQLite + FTS5 that round-trips messages including tool calls.

## Deliverables

- `sera/tools/base.py` — `Permission` IntEnum (NONE..DANGEROUS), `ToolScope`, `Tool`, `ToolCall`, `ToolResult`, `ToolContext`.
  - `sera/tools/registry.py` — idempotent `register()`, `pkgutil`-based auto-discovery on first access.
  - `sera/tools/dispatcher.py` — `execute(call, ctx) -> ToolResult` with exception capture.
  - 5 tools under `sera/tools/impl/`: `file_read`, `file_write`, `shell_run` (with DANGEROUS classifier), `web_search` (ddgs), `memory_store` (writes to `memory.db` notes table).
  - `sera/memory/session.py` — `Session.create/load/append/search`, FTS5 virtual table + insert/delete triggers, tool-call JSON serialization.

## Files touched

`sera/tools/base.py`, `sera/tools/registry.py`, `sera/tools/dispatcher.py`, `sera/tools/impl/*.py`, `sera/memory/session.py`.

## Verification

```bash
  pytest -q tests/test_registry.py tests/test_session.py tests/test_tools_safe.py
  ```
  Expect: 7 passed.

## Dependencies

P-01.


## Notes

_Journal: decisions, blockers, commit refs go here._
