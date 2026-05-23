# P-48 — Tool-gen at runtime

## Status

done.

## Outclass claim

**The big one.** Agent authors a new tool: writes Python → `mypy --strict` → sandbox dry-run → register. Nobody ships this safely.

## Goal

Sera grows its toolbox without code review.

## Files

`sera/tools/genesis.py`, `~/.sera/tools/auto/`.

## Verification

"make me a Hacker News top-stories tool" → working tool in `~/.sera/tools/auto/` and listed in `sera tools` after one turn.

## Dependencies

P-44, P-22.


## Notes

2026-05-23: sera/tools/genesis.py — full pipeline: validate_name (regex ^[a-z_][a-z0-9_]{1,63}$) + validate_permission → ast_safety_scan blocks eval/exec/compile/__import__/__builtins__/__globals__/globals/locals/vars + subprocess shell=True → render_file (templated tool file with handler_body indented + register() call at module level) → mypy --strict (graceful skip if not installed) → sandbox_dry_run (P-44 LocalSubprocessSandbox imports the file in clean subprocess) → live import via importlib spec_from_file_location (triggers register()) → registry confirmation. Failures roll back the written file. DEFAULT_AUTO_DIR = ~/.sera/tools/auto. list_auto_tools/delete_auto_tool helpers. Verification: hn_top_stories ToolSpec → genesis() → file written + tool in all_tools(). 33 tests, 957 total.
