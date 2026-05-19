# Sera — Autonomous Agent

Hermes brain × OpenHuman body × council judgment × Kimi consolidation.

**Master plan:** [`STEP-BY-STEP.md`](./STEP-BY-STEP.md) — 100 phases, 10 epochs, 31-item outclass matrix.
**Per-phase mirror:** [`phases/`](./phases/) — one slim file per phase, status-tracked.
**Original design doc:** [`BLUEPRINT.md`](./BLUEPRINT.md) — preserved for lineage; STEP-BY-STEP supersedes it.

## Status

P-01..P-05 done. P-06 (Anthropic prompt-cache stability, freeze-at-start) is next.

What's live today: CLI core with ReAct loop, 5 tools, SQLite/FTS5 session, OpenAI + Anthropic adapters, approval gate, redacted tool-arg echo, mid-turn context compression with `[CONTEXT COMPACTION — REFERENCE ONLY]` framing + streaming `<context>` scrubber + ContextOverflow retry. 30 pytests green.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11+ required.

## First run

```bash
sera setup                     # pick provider, paste API key (saved to OS keychain)
sera chat                      # interactive REPL
```

Inside the REPL:

```
you › read pyproject.toml and tell me what python version is required
you › :search python            # FTS5 recall across all sessions
you › exit
```

API keys may also come from env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) — they
win over the keychain.

## Tools shipped (week 1)

| Tool | Permission | Notes |
|---|---|---|
| `file_read` | READ_ONLY | Workspace-sandboxed, 256 KB cap |
| `file_write` | WRITE | Workspace-sandboxed |
| `shell_run` | EXECUTE → DANGEROUS on `rm -rf`, `sudo`, `dd`, fork bombs, etc. |
| `web_search` | READ_ONLY | DuckDuckGo via `ddgs` (no API key) |
| `memory_store` | WRITE | Pins a fact to `~/.sera/memory.db` notes table |

`DANGEROUS` tool calls block on an approval prompt before execution.

## Tests

```bash
pytest
```

Covers FTS5 round-trip, tool auto-discovery, file-read workspace escape, and the
dangerous-shell classifier.

## Layout

```
sera/
  agent/loop.py              ReAct loop + compaction wiring
  context/compressor.py      Mid-turn compaction w/ Remaining Work framing
  context/scrubber.py        Streaming <context> scrubber (boundary-safe)
  context/tokens.py          Token estimator (tiktoken + fallback)
  llm/adapters/              openai_adapter.py, anthropic_adapter.py
  llm/base.py                LLM protocol + ContextOverflow
  llm/router.py              profile → adapter
  tools/base.py              Tool, Permission, ToolScope, ToolContext
  tools/registry.py          auto-discovery + register
  tools/dispatcher.py        execute(call, ctx) → ToolResult
  tools/impl/                file_read, file_write, shell_run, web_search, memory_store
  memory/session.py          SQLite + FTS5 session store
  safety/approval.py         CLI approval gate (Tauri swap point at P-64)
  cli/main.py                sera chat / setup / tools / sessions / version
  config.py                  ~/.sera/config.yaml loader
```

## What's next

See [`STEP-BY-STEP.md`](./STEP-BY-STEP.md). Up next: **P-06 — Anthropic prompt-cache stability** (system prompt frozen at session start, ephemeral cache_control on system + last 3 tool blocks).
