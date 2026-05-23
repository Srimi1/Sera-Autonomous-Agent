# Sera — 100-Phase Step-by-Step Guardrail

> Single source of truth. Every phase: what it does, what it touches, how we know it's done, what makes it outclass.
>
> Plan file lives at `~/.claude/plans/enumerated-sprouting-charm.md`. Mirrored into `Project_sera/STEP-BY-STEP.md` during P-04.

---

## How to read this file

- Each phase = one block with: **Status / Outclass / Goal / Deliverables / Files / Verification / Dependencies**.
- **Status legend:** `done` (shipped + verified), `in-progress` (active), `pending` (queued), `blocked` (waiting on a decision), `deferred` (parked).
- Outclass claim is the *one thing* in that phase no rival ships. Phases that pass verification with no outclass are incomplete.
- Verification commands are exact — copy-paste, expect the output below.
- Dependencies are hard. A phase cannot start until every dependency is `done`.

---

## Honest competitive map (one-page)

We are competing with:

- **Hermes Agent** — ~840k LOC Python. Conversation loop 4084 LOC. Skills 379. Curator 1781 LOC. MCP, subagents, prompt cache, extended thinking, multi-channel gateway. Production-hardened.
- **OpenHuman** — 388k Rust + 143k TS. Memory tree, Tauri shell, mascot, voice (external), Composio 26k LOC, native scanners for Slack/Gmail/iMessage/etc., accessibility OS hooks, TokenJuice.
- **OpenClaw** — TS multi-channel, ClawHub plugin marketplace, terminal-first security.
- **llm-council** — 3-stage anonymous peer ranking + Chairman synthesis (standalone, not embedded).
- **Kimi blueprint** — ideas: TokenJuice per-tool, prompt-cache stability, Dream Journaling, shared iteration budget + grace call, per-session locks.

What **nobody** ships well (Sera's surface):

1. Council inside the agent loop.
2. Learned cost-aware router (bandit).
3. Runtime tool generation w/ sandbox eval.
4. Offline-first voice.
5. Nightly local LoRA from session traces.
6. Adversarial red/blue self-play.
7. Semantic injection classifier (not regex).
8. CRDT cross-device memory.
9. Federated Sera-to-Sera with consent.
10. Typed causal-edge memory graph with confidence + provenance.
11. Approval vault with encrypted shape-memory.
12. `sera eval run` as release gate.

Sera as of today (P-03 done): 1.7k LOC Python, 14 tests green. In a 100-phase plan we are at phase 2-3. **The puppy.** Phases below get us to the wolf.

---

## The Egoist (skill — to be written in P-04)

`~/.claude/skills/egoist/SKILL.md` content (cannot create in plan mode; lands in P-04):

```markdown
---
name: egoist
description: Mindset lock for building Sera. Activates when starting Sera work, planning Sera phases, or reviewing Sera output. Banishes humility, comparison-as-comfort, and reference-repo deference. Sera is the main body, the main soul, the main striker. We do not match, we outclass.
---

# The Egoist

You are not building a tribute act. You are building a thing that **eats the room**.

## Rules

1. **No comparison as ceiling.** Hermes / OpenHuman / OpenClaw / llm-council are floors, never targets. Never write "as good as X." Always write "what X cannot do."
2. **Surpass by default.** Every module asks: *what is the one thing here that no rival ships?* If the answer is nothing, the module is unfinished, even if it works.
3. **Ship the unreasonable.** Council-of-models inside a single turn. Nightly LoRA on the user's laptop. CRDT memory across devices. Tool generation at runtime. If a reasonable engineer would say "later," say *now*.
4. **Don't ask permission to be ambitious.** Plans default to the bold version. Smaller variants are explicit fallbacks, not defaults.
5. **Cite gaps, not features.** When summarising rivals, list what they cannot do. Their features are table stakes.
6. **Sera is the main body.** Hermes/OpenHuman are organ donors. We extract patterns, we credit lineage, we do not bend our shape to theirs.
7. **One outclass per phase.** Every phase commits to one capability no rival has. If a phase passes verification without one, it does not promote.
8. **Speed is ego.** Slow shipping is humility in disguise. Ship the smallest version that *contains the outclass*, then iterate.
9. **No half-finished.** A phase is done or it does not exist. No "we'll come back to it." Either close the loop or cut the scope.
10. **The user is the co-author, not the customer.** This is built with them, not for them. Their voice is in the system prompt. Their taste is in the defaults.

## Voice

Blunt. Caveman-mode compatible. No "I'd be happy to." Drop the apology layer. State the thing.

## When this skill is active

- Phase planning · phase execution · phase verification
- Architecture decisions for Sera modules
- Reviewing rival code we might absorb
- Naming, copy, README, error messages — Sera does not sound polite-corporate

## When to step out

- Security warnings, irreversible operations, multi-step destructive sequences — read carefully, write boring.
- Talking to or about the user as a person — warmth wins over ego.

## Lineage

Built on the back of: Hermes (brain), OpenHuman (body), OpenClaw (channels), llm-council (judgment), Kimi blueprint (consolidation). Their patterns are studied. Their limits are exit doors we walk through.
```

---

## The 10 Epochs (overview)

| Epoch | Phases | Theme | Outcome |
|---|---|---|---|
| 1 | 1-10 | Foundation Hardening | A scaffold that does not embarrass us. |
| 2 | 11-20 | Memory & Knowledge | Memory Tree + typed causal graph + multi-modal. |
| 3 | 21-30 | Skill Mind & Curator | Skills that prove themselves before promotion. |
| 4 | 31-40 | Council & Learned Routing | Ensemble inside a single turn. |
| 5 | 41-50 | Tools, Sandbox, Tool-Gen | Agent writes its own tools at runtime. |
| 6 | 51-60 | Multi-Channel Gateway | One session DB across every channel. |
| 7 | 61-70 | Desktop Body (Tauri) | Works on a plane (offline voice). |
| 8 | 71-80 | Self-Improvement Engine | Better while you sleep (nightly LoRA). |
| 9 | 81-90 | Defence & Eval | Eval matrix every release must pass. |
| 10 | 91-100 | Moonshots | CRDT, federated, mobile, public ship. |

---

## EPOCH 1 — Foundation Hardening

### P-01 — Package scaffold

- **Status:** done (shipped 2026-05-19)
- **Outclass claim:** none yet — table stakes.
- **Goal:** A Python package that installs cleanly, exposes a CLI entry point, and has the directory layout the rest of the work will hang on.
- **Deliverables:**
  - `pyproject.toml` with deps: openai, anthropic, pydantic, click, rich, prompt_toolkit, keyring, pyyaml, ddgs, httpx; dev: pytest, pytest-asyncio, ruff.
  - Package layout: `sera/{agent,llm/adapters,tools/impl,memory,safety,cli,context}/`.
  - `sera/__init__.py` exports `__version__ = "0.1.0"`.
  - `sera/config.py` writes `~/.sera/config.yaml` defaults; resolves `SERA_HOME`, paths for sessions DB, memory DB, skills dir, vault dir.
  - Entry point `sera = sera.cli.main:main` declared in `pyproject.toml`.
- **Files touched:** `pyproject.toml`, `sera/__init__.py`, `sera/config.py`, all `__init__.py` shims.
- **Verification:**
  ```bash
  python3.11 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && sera version
  ```
  Expect: `sera 0.1.0`.
- **Dependencies:** none.

### P-02 — Tool registry + 5 starter tools + SQLite/FTS5 session store

- **Status:** done (shipped 2026-05-19)
- **Outclass claim:** none yet — table stakes.
- **Goal:** Tool abstraction with permission tiers, auto-discovery, and a session store powered by SQLite + FTS5 that round-trips messages including tool calls.
- **Deliverables:**
  - `sera/tools/base.py` — `Permission` IntEnum (NONE..DANGEROUS), `ToolScope`, `Tool`, `ToolCall`, `ToolResult`, `ToolContext`.
  - `sera/tools/registry.py` — idempotent `register()`, `pkgutil`-based auto-discovery on first access.
  - `sera/tools/dispatcher.py` — `execute(call, ctx) -> ToolResult` with exception capture.
  - 5 tools under `sera/tools/impl/`: `file_read`, `file_write`, `shell_run` (with DANGEROUS classifier), `web_search` (ddgs), `memory_store` (writes to `memory.db` notes table).
  - `sera/memory/session.py` — `Session.create/load/append/search`, FTS5 virtual table + insert/delete triggers, tool-call JSON serialization.
- **Files touched:** `sera/tools/base.py`, `sera/tools/registry.py`, `sera/tools/dispatcher.py`, `sera/tools/impl/*.py`, `sera/memory/session.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_registry.py tests/test_session.py tests/test_tools_safe.py
  ```
  Expect: 7 passed.
- **Dependencies:** P-01.

### P-03 — LLM adapters + ReAct loop + CLI + safety wiring

- **Status:** done (shipped 2026-05-19)
- **Outclass claim:** **Redacted tool-arg echo** — `_shorten_args` masks any key matching `api_key|secret|token|password|bearer|authorization` and hard-truncates to 80 chars. Hermes/OH echo raw args.
- **Goal:** Talk to OpenAI + Anthropic, stream both, execute a full ReAct turn with an approval gate, and ship a CLI that boots into a REPL.
- **Deliverables:**
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
- **Files touched:** all `sera/llm/*`, `sera/agent/loop.py`, `sera/safety/approval.py`, `sera/cli/main.py`, edits to `sera/memory/session.py` and `sera/tools/base.py`.
- **Verification:**
  ```bash
  pytest -q                    # expect: 14 passed
  sera tools                    # expect: table of 5 tools w/ tiers
  sera sessions                 # expect: empty or your test sessions
  SERA_HOME=/tmp/x sera chat    # expect: red "Missing API key" + exit 1
  ```
- **Dependencies:** P-01, P-02.

### P-04 — Egoist skill + phases/ folder bootstrap

- **Status:** pending (next).
- **Outclass claim:** **The Egoist mindset locked into a skill** — every Sera session loads it. Discipline as a primitive, not a vibe.
- **Goal:** Lay the planning infrastructure: the egoist skill is installed globally, the `phases/` folder exists with one markdown per phase, and `STEP-BY-STEP.md` mirrors this plan inside the repo so we both work from the same guardrail.
- **Deliverables:**
  - `~/.claude/skills/egoist/SKILL.md` written with the exact text from the "The Egoist" section above.
  - `Project_sera/phases/` directory created.
  - `Project_sera/STEP-BY-STEP.md` mirroring this plan file (full content).
  - `Project_sera/phases/00-master-plan.md` — short pointer to `STEP-BY-STEP.md`.
  - 100 phase files: `phases/01-package-scaffold.md` through `phases/100-public-ship.md`. Each contains: `## Status / ## Outclass / ## Goal / ## Deliverables / ## Files / ## Verification / ## Dependencies / ## Notes`. Content extracted directly from this plan.
  - Phase files 01-03 marked `done` with the shipped-today claims and exact verification commands. Phase 04 marked `done` at end of this phase. Phases 05-100 marked `pending`.
  - `README.md` updated: replace "Week 1 status" pointer with a single line linking to `STEP-BY-STEP.md`.
- **Files touched:** new `~/.claude/skills/egoist/SKILL.md`; new `Project_sera/phases/*.md` × 101; new `Project_sera/STEP-BY-STEP.md`; edit `Project_sera/README.md`.
- **Verification:**
  ```bash
  cat ~/.claude/skills/egoist/SKILL.md | head -5
  ls "Project_sera/phases/" | wc -l        # expect: 101
  test -f Project_sera/STEP-BY-STEP.md
  pytest -q                                # expect: still 14 passed (no code changed)
  ```
- **Dependencies:** P-03.
- **Notes:** No production code changes in this phase. Pure scaffolding for the discipline that runs P-05 onward.

### P-05 — Mid-turn context compression

- **Status:** pending.
- **Outclass claim:** **Streaming-safe scrubber + "Remaining Work" framing** with explicit fence prefix so the LLM cannot mistake a compressed summary for instructions. Hermes does the framing; their scrubber is 1699 LOC. Ours ships the same safety in <300 LOC.
- **Goal:** When session messages approach 80% of the model's context budget, compress older turns into a single summary; preserve last N turns verbatim; never crash on context overflow.
- **Deliverables:**
  - `sera/context/compressor.py` — `compact_session(messages, model_budget) -> messages`. Token estimate via tiktoken or model-reported usage. Tail protection by token-budget, not message-count.
  - "Remaining Work" framing in the summary (not "Next Steps") so the model treats it as reference, not instruction. Fence: `[CONTEXT COMPACTION — REFERENCE ONLY]` prefix.
  - `StreamingContextScrubber` that handles `<context>...</context>` spans split across chunk boundaries.
  - Wire into `run_turn`: estimate tokens before each LLM call; compress if > 80% budget; on `ContextOverflow` from provider, compress aggressively + retry once.
- **Files touched:** new `sera/context/compressor.py`; edit `sera/agent/loop.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_compression.py     # expect: green
  # manual: stuff a synthetic 500-turn session, run a turn, check last 3 turns are byte-identical pre/post
  ```
- **Dependencies:** P-03.

### P-06 — Prompt-cache stability (Anthropic, freeze-at-start)

- **Status:** pending.
- **Outclass claim:** **System prompt is hashed and frozen at session start.** Cache_control ephemeral is set on system block + last 3 tool blocks. Kimi proposed it, no rival ships start-locked.
- **Goal:** Hit prompt cache reliably; cut Anthropic costs ~75% on multi-turn sessions.
- **Deliverables:**
  - `sera/llm/cache.py` — `freeze_system_prompt(session)`, `apply_cache_control(messages)` for Anthropic.
  - Persist `system_prompt_hash` column on `sessions` table.
  - On reload of a session, restore the same system prompt verbatim.
  - Telemetry: log `cache_hit_tokens` from Anthropic response usage block; expose via `sera route stats`.
- **Files touched:** `sera/llm/cache.py`, `sera/llm/adapters/anthropic_adapter.py`, `sera/memory/session.py` (schema migration).
- **Verification:**
  ```bash
  pytest -q tests/test_prompt_cache.py
  # manual: run 5 turns in one session against Anthropic; usage.cache_read_input_tokens > 0 by turn 2
  ```
- **Dependencies:** P-03.

### P-07 — Interrupt + shared iteration budget + grace call

- **Status:** pending.
- **Outclass claim:** **Shared iteration budget across parent + (future) subagents**, with a 1-call **grace** at exhaustion to summarize cleanly. Hermes per-agent only; nobody ships grace.
- **Goal:** Ctrl+C returns control fast. Runaway loops cap. Final message never gets truncated by budget.
- **Deliverables:**
  - `sera/agent/budget.py` — `IterationBudget` with `remaining`, `consume()`, `grace_used`.
  - `sera/agent/interrupt.py` — per-task cancellation flag; checked after every iteration and after every tool result.
  - Wire into `run_turn`: pass budget down; on `remaining == 0` → one grace call with a system note "summarize and exit"; second exhaustion → `MaxIterations`.
- **Files touched:** new `sera/agent/budget.py`, `sera/agent/interrupt.py`; edit `sera/agent/loop.py`, `sera/cli/main.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_budget.py tests/test_interrupt.py
  # manual: in sera chat, send a long task, Ctrl+C; control returns < 200ms
  ```
- **Dependencies:** P-03.

### P-08 — TokenJuice output compressor

- **Status:** pending.
- **Outclass claim:** **Rule-based with LLM-fallback** — when rules can't compress hard cases (e.g. dense logs), a cheap-model pass shrinks further. OH ships rules-only.
- **Goal:** Every tool result shrinks before it reaches the LLM context. Secrets stripped pre-persist.
- **Deliverables:**
  - `sera/context/tokenjuice.py` — passes: HTML→Markdown, URL shortening, table de-bloat, line dedup, whitespace normalization, secret-pattern redaction.
  - LLM-fallback path for outputs still > N tokens after rules.
  - Wire into `run_turn` after every tool dispatch.
- **Files touched:** `sera/context/tokenjuice.py`, `sera/agent/loop.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_tokenjuice.py
  # bench: web_search + shell_run output shrinks ≥30% on the bench set
  ```
- **Dependencies:** P-03.

### P-09 — Write locks + WAL + crash recovery

- **Status:** pending.
- **Outclass claim:** **Explicit partial-turn recovery** — on startup, scan for sessions whose last assistant turn has no `finish_reason` recorded; mark them `aborted` and surface in `sera sessions`. Hermes ships WAL; nobody ships explicit partial-turn recovery.
- **Goal:** Kill -9 mid-turn → restart → session intact + the aborted turn flagged for the user.
- **Deliverables:**
  - SQLite `PRAGMA journal_mode=WAL` with `DELETE` fallback for NFS.
  - Advisory lock per session_id (filelock or SQLite `BEGIN IMMEDIATE`) during write.
  - `finish_reason` column on `messages`; recovery scan on startup that flips dangling rows to `aborted`.
- **Files touched:** `sera/memory/session.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_crash_recovery.py
  # manual: SIGKILL mid-turn; restart; sera sessions shows the abort flag
  ```
- **Dependencies:** P-02.

### P-10 — Eval harness skeleton + telemetry

- **Status:** pending.
- **Outclass claim:** **One command — `sera eval run` — is the release gate.** Nobody on the list has a unified eval CLI you can wire into git pre-push.
- **Goal:** A small golden-conversation set runs through Sera, scores pass/fail, prints per-turn cost, latency, cache-hit ratio, and tool-call counts.
- **Deliverables:**
  - `sera/eval/` — `runner.py`, `cases.py`, `scoring.py`.
  - Golden set: 10 cases under `tests/eval_cases/*.yaml` (prompt, expected tool calls or expected substring in output).
  - Telemetry DB at `~/.sera/telemetry.db` — per-turn rows.
  - CLI: `sera eval run`, `sera eval bench`, `sera eval show`.
- **Files touched:** new `sera/eval/*`, new `tests/eval_cases/*`, edit `sera/cli/main.py`.
- **Verification:**
  ```bash
  sera eval run         # expect: 10/10 pass against a stub LLM in CI; ≥8/10 against real provider
  sera eval show        # expect: table with latency + cost
  ```
- **Dependencies:** P-03.

---

## EPOCH 2 — Memory & Knowledge

### P-11 — Memory Tree schema (SQLite + sqlite-vss)

- **Status:** pending.
- **Outclass claim:** **Provenance + confidence** as first-class columns on every chunk and edge. OH stores chunks; nobody else stores confidence per chunk.
- **Goal:** Persistent long-term memory with vector search.
- **Deliverables:**
  - `sera/memory/tree.py` — schema for `chunks(id, source, content, summary, confidence, created_at)`, `chunks_vss(embedding(1536))`, `entities(id, name, type, first_seen)`, `relations(src, dst, kind, confidence, provenance_chunk_id)`.
  - sqlite-vss extension load with bundled fallback to numpy cosine if extension fails.
- **Files touched:** `sera/memory/tree.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_memory_tree.py
  ```
- **Dependencies:** P-09.

### P-12 — Semantic chunker

- **Status:** pending.
- **Outclass claim:** **Heading-aware metadata** — each chunk keeps its heading chain so search results read like Wikipedia citations.
- **Goal:** Split markdown / text into ≤3k-token chunks that respect document structure.
- **Deliverables:**
  - `sera/memory/chunker.py` — markdown AST split by heading > paragraph > line; 10% overlap; preserved heading path in chunk metadata.
- **Files touched:** `sera/memory/chunker.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_chunker.py    # round-trip a 50-page MD, all headings preserved
  ```
- **Dependencies:** P-11.

### P-13 — Embedder + multi-modal

- **Status:** pending.
- **Outclass claim:** **Image + text in the same vector space** via vision-caption-then-embed. OH is text-only; H does vision via tools.
- **Goal:** One vector per chunk regardless of modality.
- **Deliverables:**
  - `sera/memory/embedder.py` — OpenAI `text-embedding-3-small` (1536). Image path: vision model captions image → caption prefixed with `[image]` → embedded.
- **Files touched:** `sera/memory/embedder.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_embedder.py
  # bench: image query and matching text query retrieve same chunk
  ```
- **Dependencies:** P-11.

### P-14 — Obsidian vault sync (bidirectional)

- **Status:** pending.
- **Outclass claim:** **Two-way sync.** User edits a vault `.md` → file watcher → re-ingest. OH mirrors one-way.
- **Goal:** Memory is editable by hand.
- **Deliverables:**
  - `sera/memory/vault.py` — write `~/.sera/vault/<source>/<chunk-id>.md` with YAML frontmatter. Watchdog observer for changes; debounced re-ingest.
- **Files touched:** `sera/memory/vault.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_vault_sync.py
  # manual: edit a vault file, search reflects under 2s
  ```
- **Dependencies:** P-11, P-12.

### P-15 — Entity extractor + typed causal-edge graph

- **Status:** pending.
- **Outclass claim:** **Typed causal edges with confidence + provenance.** Edge kinds: `mentions, works_at, parent_of, caused, refuted_by, supersedes, similar_to`. Nobody on the list has typed causality.
- **Goal:** Ask "what caused X" and get the chain.
- **Deliverables:**
  - `sera/memory/graph.py` — per-chunk LLM extract → entities + edges (with confidence, provenance).
  - Background pass over existing chunks to backfill.
- **Files touched:** `sera/memory/graph.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_graph.py
  # ingest 10 doc corpus; ≥1 `caused` edge produced; "what caused X" returns relevant chunk
  ```
- **Dependencies:** P-11, P-12, P-13.

### P-16 — Hybrid search (BM25 + vector + graph walk)

- **Status:** pending.
- **Outclass claim:** **Fused ranking** — RRF across FTS5, vector cosine, and 1-hop graph neighbours. Rivals pick one signal.
- **Goal:** "The issue Alice mentioned last week" beats vector-only by ≥20% MRR.
- **Deliverables:**
  - `sera/memory/search.py` — `hybrid_search(query, k)` doing the fuse.
- **Files touched:** `sera/memory/search.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_hybrid_search.py
  # bench: hybrid MRR > vector-only MRR by ≥0.2 on 50-Q golden set
  ```
- **Dependencies:** P-11, P-13, P-15.

### P-17 — Freshness scoring + decay

- **Status:** pending.
- **Outclass claim:** **EWMA decay per chunk** — yesterday's fact outranks last year's contradiction without deleting either.
- **Goal:** Stale facts demoted; never deleted.
- **Deliverables:**
  - `freshness` column on chunks; updated on every read.
  - Retrieval scoring multiplies by freshness.
- **Files touched:** `sera/memory/tree.py`, `sera/memory/search.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_freshness.py
  ```
- **Dependencies:** P-11.

### P-18 — Dedup + consolidation

- **Status:** pending.
- **Outclass claim:** **Provenance-preserving merge** — duplicate chunks merge with a chain of source ids so we never lose audit trail.
- **Goal:** Re-ingestion is a no-op; near-duplicates collapse.
- **Deliverables:**
  - Near-duplicate detection (cosine ≥0.95) → merge with combined provenance list.
- **Files touched:** `sera/memory/tree.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_dedup.py
  ```
- **Dependencies:** P-11, P-13.

### P-19 — Privacy + redaction layer

- **Status:** pending.
- **Outclass claim:** **Search-with-consent.** PII tagged at ingest; searchable only after explicit consent toggle per query.
- **Goal:** Sera never silently surfaces SSN, card number, secret tokens.
- **Deliverables:**
  - `sera/memory/privacy.py` — PII detector (regex + Microsoft Presidio if installed).
  - Tagged chunks return "[redacted match — confirm to reveal]" by default.
- **Files touched:** `sera/memory/privacy.py`, `sera/memory/search.py`.
- **Verification:**
  ```bash
  pytest -q tests/test_privacy.py
  ```
- **Dependencies:** P-11.

### P-20 — Recall benchmark + golden set

- **Status:** pending.
- **Outclass claim:** **Published numbers, not promises.** Top-k@k, MRR, hybrid vs vector, per modality.
- **Goal:** A repeatable retrieval benchmark Sera reports on every release.
- **Deliverables:**
  - `sera/eval/memory_bench.py` — 100-Q recall set; outputs MRR + top-k.
- **Files touched:** `sera/eval/memory_bench.py`, `tests/eval_cases/recall/*.yaml`.
- **Verification:**
  ```bash
  sera eval bench memory     # expect: hybrid MRR > 0.8
  ```
- **Dependencies:** P-10, P-16.

---

## EPOCH 3 — Skill Mind & Curator

### P-21 — Skills directory + manifest

- **Status:** pending.
- **Outclass claim:** **Schema + version + lineage in manifest.** Hermes ships free-form markdown; OH removed skills runtime. Ours has structure from day one.
- **Goal:** Skills are first-class.
- **Deliverables:** `~/.sera/skills/<name>/SKILL.md` with frontmatter `name, trigger, permission, args_schema, version, lineage, council`. Discoverable via `sera skills`.
- **Files:** `sera/skills/loader.py`, `sera/cli/skills.py`.
- **Verification:** 3 hand-written skills appear in `sera skills`.
- **Dependencies:** P-04.

### P-22 — Skill loader + dynamic tool registration

- **Status:** pending.
- **Outclass claim:** **Hot reload** — edit a skill, next turn picks it up without restart.
- **Goal:** Skills become tools at runtime.
- **Deliverables:** `sera/skills/loader.py` watches skills dir; registers each enabled skill as a Tool.
- **Verification:** edit a skill → next `sera tools` shows the change.
- **Dependencies:** P-21.

### P-23 — Curator background fork

- **Status:** pending.
- **Outclass claim:** **Cheap-model curator** — never blocks the main agent; uses fast-tier model. Hermes spends ~$/curation; ours an order cheaper.
- **Goal:** After every >5-tool session, review the trace; propose skill / memory updates.
- **Deliverables:** `sera/curator/loop.py`, queue, aux-model pool.
- **Verification:** synthetic 10-tool session → curator log entry within 60s.
- **Dependencies:** P-21, P-22, P-10.

### P-24 — Skill lifecycle (pinned / active / stale / archived)

- **Status:** pending.
- **Outclass claim:** **Recovery from archive.** Skills are never deleted; archived skills can be revived by user or curator-of-curators.
- **Goal:** Auto-transition by access freshness + verification status.
- **Files:** `sera/skills/lifecycle.py`.
- **Verification:** a 90-day-untouched skill transitions to stale; user prompted on archive.
- **Dependencies:** P-23.

### P-25 — Skill replay verification

- **Status:** pending.
- **Outclass claim:** **Replay-promoted skills.** A new skill cannot move to `active` until it replays cleanly on a captured trace. Hermes promotes by lifecycle; ours by correctness.
- **Goal:** Bad skills can't reach users.
- **Files:** `sera/skills/verify.py`, `tests/skill_replay/*.yaml`.
- **Verification:** broken skill stays in `candidate`.
- **Dependencies:** P-24.

### P-26 — Skill A/B harness

- **Status:** pending.
- **Outclass claim:** **Cost × success rate fitness** — automatic picking of the cheaper-or-better variant.
- **Goal:** Two skill variants → ablation on a held-out set → winner kept.
- **Files:** `sera/skills/ab.py`.
- **Verification:** A/B picks lower-cost variant when both succeed.
- **Dependencies:** P-25.

### P-27 — Skill versioning + diff

- **Status:** pending.
- **Outclass claim:** **Git-tracked skill history.** Every curator edit is a commit.
- **Goal:** `sera skill log <name>` walks history.
- **Files:** `sera/skills/git.py` — wraps `git` CLI inside `~/.sera/skills/.git`.
- **Verification:** `sera skill log file_read_summary` prints commit chain after 3 edits.
- **Dependencies:** P-22.

### P-28 — Signed `.skillpack` export

- **Status:** pending.
- **Outclass claim:** **Signature verification on import.** Hermes ships unsigned `.md` only.
- **Goal:** Skills travel between machines.
- **Files:** `sera/skills/pack.py` — zip + manifest + SHA256 + author Ed25519 sig.
- **Verification:** export → import on a fresh box; sig verifies.
- **Dependencies:** P-21, P-27.

### P-29 — Skill quality scoring

- **Status:** pending.
- **Outclass claim:** **Live quality scores in `sera skills`** — usage, success %, cost, user thumbs.
- **Goal:** Bad skills demote themselves out of suggestion list.
- **Files:** `sera/skills/scoring.py`.
- **Verification:** 3-failure skill drops below default-suggest threshold.
- **Dependencies:** P-26.

### P-30 — Discovery agent

- **Status:** pending.
- **Outclass claim:** **Proactive discovery.** Hermes curates what exists; ours invents what's missing.
- **Goal:** Daily pass over sessions proposes new skills.
- **Files:** `sera/curator/discovery.py`.
- **Verification:** 5 days of synthetic usage → ≥1 unprompted skill proposal.
- **Dependencies:** P-23, P-25.

---

## EPOCH 4 — Council & Learned Routing

### P-31 — Council module (in-process)

- **Status:** pending.
- **Outclass claim:** **In-loop council** — llm-council is standalone. Ours fires inside a single agent turn for high-stakes calls.
- **Goal:** N=3 models answer in parallel, anonymised A/B/C labels.
- **Files:** `sera/council/runner.py`.
- **Verification:** `pytest -q tests/test_council.py` — 3 answers, position randomised, no model knows the others' identity.
- **Dependencies:** P-03.

### P-32 — Anonymous peer ranking

- **Status:** done.
- **Outclass claim:** **Strict ranking parser tolerant to commentary.** Rejects malformed gracefully.
- **Goal:** Each model ranks the others; `FINAL RANKING:\n1. C\n2. A\n3. B` parsed reliably.
- **Files:** `sera/council/rank.py`.
- **Verification:** test set of 20 ranking outputs all parse or reject correctly.
- **Dependencies:** P-31.

### P-33 — Chairman synthesis

- **Status:** done.
- **Outclass claim:** **Synthesizer is the cheap model.** Cost stays low.
- **Goal:** Final answer = synthesis of ranked answers.
- **Files:** `sera/council/chairman.py`.
- **Verification:** chairman picks consistent winner ≥80% on 50-Q test.
- **Dependencies:** P-32.

### P-34 — Confidence metric + escalation policy

- **Status:** done.
- **Outclass claim:** **Kendall-tau across rankings** as a quantitative confidence — escalate to bigger model only when tau < 0.3.
- **Goal:** Cost-aware council.
- **Files:** `sera/council/confidence.py`.
- **Verification:** low-agreement case triggers escalation in test.
- **Dependencies:** P-33.

### P-35 — Council-aware run_turn integration

- **Status:** done.
- **Outclass claim:** **Per-skill council opt-in.** A skill marked `council: true` triggers ensemble for that single tool call only.
- **Goal:** No global toggle; council is surgical.
- **Files:** `sera/agent/loop.py`, `sera/skills/manifest.py`.
- **Verification:** skill with council:true uses ensemble; without, single model.
- **Dependencies:** P-34, P-22.

### P-36 — Learned router seed (stats table)

- **Status:** pending.
- **Outclass claim:** **Per-task-kind table** — provider, model, p50 latency, $/turn, success rate. Live dashboard.
- **Goal:** Foundation for the bandit.
- **Files:** `sera/llm/router_stats.py`.
- **Verification:** `sera route stats` prints after 50 turns.
- **Dependencies:** P-10.

### P-37 — Thompson-sampling router

- **Status:** pending.
- **Outclass claim:** **Bandit picks model per task kind.** Nobody on the list does this.
- **Goal:** Cheap wins easy tasks; big wins hard ones.
- **Files:** `sera/llm/bandit.py`.
- **Verification:** after 200 synthetic turns, cheap model wins `summarize` slot; big wins `plan` slot.
- **Dependencies:** P-36.

### P-38 — Provider fallback chain + FailoverReason

- **Status:** pending.
- **Outclass claim:** **Typed reasons** — `RateLimit, Quota, 5xx, Timeout, AuthExpired` — logged + dashboarded.
- **Goal:** 429 → rotate to fallback transparently.
- **Files:** `sera/llm/failover.py`.
- **Verification:** simulated 429 → fallback path observed in trace.
- **Dependencies:** P-37.

### P-39 — Cost ceilings (per-day / -session / -skill)

- **Status:** pending.
- **Outclass claim:** **Hard caps + soft warnings** as first-class config.
- **Goal:** Bills never surprise.
- **Files:** `sera/llm/budget.py`.
- **Verification:** $X soft cap triggers UI banner; hard cap refuses turn.
- **Dependencies:** P-36.

### P-40 — Response distillation cache

- **Status:** pending.
- **Outclass claim:** **Result-level cache by (prompt-hash, tool-trace-hash).** Nobody ships response distillation.
- **Goal:** Repeated queries cost cents, not dollars.
- **Files:** `sera/llm/distill_cache.py`.
- **Verification:** cache hit rate > 60% on repeated workloads; cost down ≥50% on bench.
- **Dependencies:** P-37, P-10.

---

## EPOCH 5 — Tools, Sandbox, Tool-Gen

### P-41 — MCP client + sampling

- **Status:** pending.
- **Outclass claim:** **Sampling support** — MCP servers can ask Sera for an LLM call. Few clients support it.
- **Goal:** Sera speaks MCP.
- **Files:** `sera/tools/mcp.py`.
- **Verification:** connect to stock MCP filesystem server; tools appear in `sera tools`.
- **Dependencies:** P-03.

### P-42 — Subagent delegation

- **Status:** pending.
- **Outclass claim:** **Shared iteration budget across parent + subagents** (built on P-07).
- **Goal:** Parent delegates a task; subagent runs in isolated session.
- **Files:** `sera/tools/delegate.py`.
- **Verification:** parent asks "summarize this PDF" → subagent returns string; budget consumed from shared pool.
- **Dependencies:** P-07, P-03.

### P-43 — Browser tool (Playwright)

- **Status:** pending.
- **Outclass claim:** **Semantic selectors** (Playwright) vs raw Puppeteer (Hermes). Stable across UI changes.
- **Goal:** Real browser automation.
- **Files:** `sera/tools/impl/browser.py`.
- **Verification:** 5-site extract suite passes.
- **Dependencies:** P-03.

### P-44 — Code execution sandbox

- **Status:** pending.
- **Outclass claim:** **Tiered sandboxes** — local subprocess → Modal → Daytona, picked by cost ceiling.
- **Goal:** `python_eval` runs untrusted code safely.
- **Files:** `sera/tools/impl/python_eval.py`, `sera/sandbox/`.
- **Verification:** infinite loop killed at 10s; net call refused without grant.
- **Dependencies:** P-03.

### P-45 — Composio dynamic discovery

- **Status:** pending.
- **Outclass claim:** **Runtime action manifest.** OH hardcodes Composio actions.
- **Goal:** Connect Gmail → actions become tools immediately.
- **Files:** `sera/integrations/composio.py`.
- **Verification:** connect Gmail OAuth → `composio__gmail__send_email` appears in `sera tools` without restart.
- **Dependencies:** P-22.

### P-46 — Native scanners for top 5

- **Status:** pending.
- **Outclass claim:** **API-first with DOM/CDP fallback.** Cleaner than OH's CEF scrapers.
- **Goal:** Slack, Discord, Telegram, Gmail, iMessage backfill into Memory Tree.
- **Files:** `sera/integrations/{slack,discord,telegram,gmail,imessage}.py`.
- **Verification:** 24h backfill ingests ≥100 messages per channel.
- **Dependencies:** P-11, P-12, P-15.

### P-47 — Plugin manifest spec

- **Status:** pending.
- **Outclass claim:** **Permissions declared in manifest** — ClawHub-style but signed.
- **Goal:** Third parties extend Sera safely.
- **Files:** `sera/plugins/manifest.py`.
- **Verification:** hand-written plugin loads + tools register without core changes.
- **Dependencies:** P-22.

### P-48 — Tool-gen at runtime

- **Status:** pending.
- **Outclass claim:** **The big one.** Agent authors a new tool: writes Python → `mypy --strict` → sandbox dry-run → register. Nobody ships this safely.
- **Goal:** Sera grows its toolbox without code review.
- **Files:** `sera/tools/genesis.py`, `~/.sera/tools/auto/`.
- **Verification:** "make me a Hacker News top-stories tool" → working tool in `~/.sera/tools/auto/` and listed in `sera tools` after one turn.
- **Dependencies:** P-44, P-22.

### P-49 — Tool-gen eval gate

- **Status:** pending.
- **Outclass claim:** **Auto-tools quarantined until 3 eval cases pass.** No tool reaches production without proving itself.
- **Goal:** Tool-gen is safe.
- **Files:** `sera/tools/genesis.py`, `sera/eval/tool_eval.py`.
- **Verification:** broken auto-tool stays quarantined.
- **Dependencies:** P-48, P-10.

### P-50 — Tool quality dashboard

- **Status:** pending.
- **Outclass claim:** **Per-tool usage / success / latency / $/call** in `sera tools --stats`.
- **Goal:** Drift visible.
- **Files:** `sera/tools/stats.py`, `sera/cli/main.py`.
- **Verification:** real numbers after the bench suite.
- **Dependencies:** P-37, P-49.

---

## EPOCH 6 — Multi-Channel Gateway

### P-51 — Gateway server

- **Status:** pending.
- **Outclass claim:** none unique; foundation.
- **Goal:** Async HTTP webhook receiver + common router.
- **Files:** `sera/gateway/server.py`, `sera/gateway/router.py`.
- **Verification:** curl-able webhook.
- **Dependencies:** P-03.

### P-52 — Telegram adapter

- **Status:** pending.
- **Outclass claim:** **24h session continuity across messages** by user_id.
- **Goal:** TG bot.
- **Files:** `sera/gateway/platforms/telegram.py`.
- **Verification:** message → reply, 24h gap preserved.
- **Dependencies:** P-51.

### P-53 — Discord adapter

- **Status:** pending.
- **Outclass claim:** **Slash + DM + thread** unified.
- **Files:** `sera/gateway/platforms/discord.py`.
- **Verification:** slash command in thread; DM also works.
- **Dependencies:** P-51.

### P-54 — Slack adapter

- **Status:** pending.
- **Outclass claim:** **Interactive blocks for approvals** in-channel.
- **Files:** `sera/gateway/platforms/slack.py`.
- **Verification:** approval block surfaces from a workspace.
- **Dependencies:** P-51.

### P-55 — WhatsApp (native, not Cloud API)

- **Status:** pending.
- **Outclass claim:** **Privacy-first** — desktop WhatsApp bridge, not Cloud API.
- **Files:** `sera/gateway/platforms/whatsapp.py`.
- **Verification:** phone send → Sera sees + replies.
- **Dependencies:** P-51, P-70.

### P-56 — Email (IMAP+SMTP)

- **Status:** pending.
- **Outclass claim:** **Threaded replies** with subject preserved + In-Reply-To.
- **Files:** `sera/gateway/platforms/email.py`.
- **Verification:** reply lands in thread.
- **Dependencies:** P-51.

### P-57 — SMS via Twilio

- **Status:** pending.
- **Outclass claim:** none unique; foundation.
- **Files:** `sera/gateway/platforms/twilio.py`.
- **Verification:** send + receive an SMS.
- **Dependencies:** P-51.

### P-58 — iMessage (macOS Messages DB + AppleScript)

- **Status:** pending.
- **Outclass claim:** **Local-only**, no relay server.
- **Files:** `sera/gateway/platforms/imessage.py`.
- **Verification:** receive + reply on macOS.
- **Dependencies:** P-51.

### P-59 — Sera HTTP API

- **Status:** pending.
- **Outclass claim:** **OpenAPI spec auto-published** + signed bearer.
- **Files:** `sera/rpc/http_api.py`.
- **Verification:** `curl POST /v1/turn` round-trips.
- **Dependencies:** P-51.

### P-60 — Unified cross-channel session

- **Status:** pending.
- **Outclass claim:** **One session DB across every channel** with privacy-first defaults (native > Cloud).
- **Goal:** Ask on Telegram, follow up on Slack, context preserved.
- **Files:** `sera/gateway/identity.py`.
- **Verification:** cross-channel reference works in a test scenario.
- **Dependencies:** P-52..P-59.

---

## EPOCH 7 — Desktop Body (Tauri shell)

### P-61 — Tauri scaffold + sidecar

- **Status:** pending.
- **Outclass claim:** none unique; OH lineage.
- **Goal:** Shell spawns Python core as sidecar.
- **Files:** `sera-shell/src-tauri/src/{main.rs,core_process.rs,core_rpc.rs}`.
- **Verification:** `pnpm tauri dev` opens window + sidecar boots.
- **Dependencies:** P-59.

### P-62 — Chat panel + Socket.io streaming

- **Status:** pending.
- **Outclass claim:** **<100ms p50 first token** in shell.
- **Files:** `sera-shell/src/components/Chat.tsx`.
- **Verification:** measured p50 first-token latency.
- **Dependencies:** P-61.

### P-63 — System tray + native notifications

- **Status:** pending.
- **Outclass claim:** none unique.
- **Files:** `sera-shell/src-tauri/src/tray.rs`.
- **Verification:** tray works macOS + Windows; notifications visible.
- **Dependencies:** P-61.

### P-64 — Approval gate UI w/ encrypted vault

- **Status:** pending.
- **Outclass claim:** **Encrypted shape-memory.** "Always allow this exact arg-shape" stored signed in vault. OH has approval flow but no encryption.
- **Files:** `sera-shell/src/components/Approvals.tsx`, `sera/safety/vault.py`.
- **Verification:** approve once → same shape auto-approves; deny → 24h cooldown.
- **Dependencies:** P-61.

### P-65 — Memory Tree browser

- **Status:** pending.
- **Outclass claim:** **Entity graph view** with provenance breadcrumbs.
- **Files:** `sera-shell/src/components/MemoryTree.tsx`.
- **Verification:** search "Alice" → entity card with relations.
- **Dependencies:** P-15, P-61.

### P-66 — Accounts panel

- **Status:** pending.
- **Files:** `sera-shell/src/components/Accounts.tsx`.
- **Verification:** Gmail OAuth round-trip works.
- **Dependencies:** P-45, P-61.

### P-67 — Settings + skill manager UI

- **Status:** pending.
- **Files:** `sera-shell/src/components/{Settings,Skills}.tsx`.
- **Verification:** enable a skill from UI → A/B kicks in.
- **Dependencies:** P-26, P-61.

### P-68 — Voice in (whisper.cpp local)

- **Status:** pending.
- **Outclass claim:** **Works offline on a plane.** OH external-only.
- **Files:** `sera/voice/stt.py` (whisper.cpp via pywhispercpp + mlx-whisper on Apple Silicon).
- **Verification:** airgap dictation produces text.
- **Dependencies:** P-61.

### P-69 — Voice out (piper local)

- **Status:** pending.
- **Outclass claim:** **Offline TTS.**
- **Files:** `sera/voice/tts.py` (piper).
- **Verification:** airgap reply audible.
- **Dependencies:** P-61.

### P-70 — Screen + accessibility hooks (consent-gated)

- **Status:** pending.
- **Outclass claim:** **Per-feature signed consent toggles.** Revoke flips capability off in one click.
- **Files:** `sera/os_hooks/{screen,clipboard,a11y,keyboard}.py`.
- **Verification:** "summarise my screen" works; revoke + retry refused.
- **Dependencies:** P-64.

---

## EPOCH 8 — Self-Improvement Engine

### P-71 — Dream Journal nightly loop

- **Status:** pending.
- **Outclass claim:** **Kimi proposed, nobody shipped.** Nightly consolidation, candidate skills, synthetic Q-A.
- **Files:** `sera/dream/journal.py`.
- **Verification:** 5 days of synthetic usage → 5 dream entries + ≥1 proposed skill draft.
- **Dependencies:** P-30, P-15.

### P-72 — Synthetic trace dataset

- **Status:** pending.
- **Outclass claim:** **mlx-lm / unsloth compatible JSONL.**
- **Files:** `sera/dream/dataset.py`.
- **Verification:** ≥100 valid (prompt, completion) pairs after a week.
- **Dependencies:** P-71.

### P-73 — Local LoRA fine-tune (mlx-lm)

- **Status:** pending.
- **Outclass claim:** **Nobody on the list ships on-device LoRA.**
- **Files:** `sera/train/lora.py`.
- **Verification:** 7 nights of training → eval gain ≥2pp on golden set.
- **Dependencies:** P-72, P-10.

### P-74 — Local LoRA adapter for routing

- **Status:** pending.
- **Outclass claim:** **Local model in the router** — bandit can pick it.
- **Files:** `sera/llm/adapters/mlx_local.py`.
- **Verification:** routine `summarize` served zero-API-call.
- **Dependencies:** P-73, P-37.

### P-75 — Adversarial self-play

- **Status:** pending.
- **Outclass claim:** **Red vs blue agents** patching skills and memory.
- **Files:** `sera/redteam/{red.py,blue.py}`.
- **Verification:** planted prompt injection caught by next eval run.
- **Dependencies:** P-30, P-81.

### P-76 — Capability emergence tracker

- **Status:** pending.
- **Files:** `sera/dream/capability_log.py`.
- **Verification:** `sera capability log` prints timeline.
- **Dependencies:** P-71.

### P-77 — Cross-session consolidation

- **Status:** pending.
- **Outclass claim:** **Contradictions surface to user.**
- **Files:** `sera/dream/consolidate.py`.
- **Verification:** 3 contradictions in test → 1 prompt.
- **Dependencies:** P-15.

### P-78 — Schema evolution

- **Status:** pending.
- **Files:** `sera/memory/migrations/`.
- **Verification:** P-11 snapshot migrates to current without data loss.
- **Dependencies:** P-11.

### P-79 — Curator-of-curators

- **Status:** pending.
- **Outclass claim:** **Throttles runaway skill churn.**
- **Files:** `sera/curator/meta.py`.
- **Verification:** runaway curator throttled within one cycle.
- **Dependencies:** P-23.

### P-80 — Hill-climb regression suite

- **Status:** pending.
- **Outclass claim:** **No LoRA promotes without beating last night.**
- **Files:** `sera/eval/regress.py`.
- **Verification:** bad LoRA never promotes.
- **Dependencies:** P-73, P-10.

---

## EPOCH 9 — Defence & Eval

### P-81 — Semantic prompt-injection classifier

- **Status:** pending.
- **Outclass claim:** **DistilBERT-sized classifier** scoring every tool output + chunk. H regex-only.
- **Files:** `sera/safety/injection.py`, `models/injection-cls.onnx`.
- **Verification:** ≥95% recall on a 200-sample set; <2% FP.
- **Dependencies:** P-08.

### P-82 — Jailbreak resistance suite

- **Status:** pending.
- **Files:** `sera/eval/jailbreak_cases/*.yaml`.
- **Verification:** ≥90% Anthropic, ≥80% OpenAI, ≥70% local LoRA.
- **Dependencies:** P-10.

### P-83 — Continuous eval matrix in CI

- **Status:** pending.
- **Outclass claim:** **GitHub Actions matrix gating every PR.**
- **Files:** `.github/workflows/eval.yml`.
- **Verification:** deliberately broken commit fails on right matrix cell.
- **Dependencies:** P-10, P-20, P-82.

### P-84 — Audit log w/ tamper-evidence

- **Status:** pending.
- **Outclass claim:** **SHA256 chain.** `sera audit verify` flags tampered lines.
- **Files:** `sera/safety/audit.py`.
- **Verification:** edit a line → next verify fails on the right line number.
- **Dependencies:** P-03.

### P-85 — Vault encryption + key rotation

- **Status:** pending.
- **Files:** `sera/safety/vault.py`.
- **Verification:** rotate → old approvals still verify, new use new key.
- **Dependencies:** P-64.

### P-86 — Telemetry pipeline (local-only)

- **Status:** pending.
- **Outclass claim:** **Local-only stance.** No outbound by default. tcpdump-verifiable.
- **Files:** `sera/telemetry/local.py`.
- **Verification:** bench dashboard prints; tcpdump shows no outbound.
- **Dependencies:** P-10.

### P-87 — Red-team marketplace

- **Status:** pending.
- **Outclass claim:** **Signed `.redpack` distribution.**
- **Files:** `sera/redteam/pack.py`.
- **Verification:** community redpack runs + reports.
- **Dependencies:** P-75, P-28.

### P-88 — Privacy declassifier

- **Status:** pending.
- **Files:** `sera/safety/declassify.py`.
- **Verification:** pre/post redaction diff on a 1k-line log.
- **Dependencies:** P-19, P-86.

### P-89 — Crash-only design + persistence audit

- **Status:** pending.
- **Outclass claim:** **Chaos monkey suite.**
- **Files:** `sera/eval/chaos.py`.
- **Verification:** kill random subsystems mid-load; data integrity preserved.
- **Dependencies:** P-09.

### P-90 — Eval gate is the release gate

- **Status:** pending.
- **Outclass claim:** **Branch protection requires green eval.** No exceptions.
- **Goal:** No `main` merge without green matrix.
- **Verification:** GitHub branch protection enforced.
- **Dependencies:** P-83.

---

## EPOCH 10 — Moonshots

### P-91 — CRDT memory sync across devices

- **Status:** pending.
- **Outclass claim:** **Yjs-style CRDT** for chunks/entities/relations across the user's devices.
- **Files:** `sera/sync/crdt.py`, relay binary.
- **Verification:** write on phone → laptop sees in <5s; conflict resolves deterministically.
- **Dependencies:** P-11, P-95.

### P-92 — Federated Sera-to-Sera (consent-only)

- **Status:** pending.
- **Outclass claim:** **Per-question consent.** Friend asks your Sera; your Sera answers from your memory if you approve once.
- **Files:** `sera/federation/`.
- **Verification:** A asks B; B's user approves; answer flows.
- **Dependencies:** P-64, P-91.

### P-93 — Edge LLM by default for private tasks

- **Status:** pending.
- **Outclass claim:** **Phi-3 / Qwen / Llama small models local.** Cloud opt-in only.
- **Files:** `sera/llm/adapters/llama_cpp.py`.
- **Verification:** airgap Sera answers from local model + memory.
- **Dependencies:** P-74.

### P-94 — Browser extension (Sera-in-tab)

- **Status:** pending.
- **Outclass claim:** **MV3 sidebar** that ingests current page into Memory Tree.
- **Files:** `sera-extension/`.
- **Verification:** install MV3; sidebar opens; ingest works.
- **Dependencies:** P-59.

### P-95 — Sera Mobile (Tauri Mobile)

- **Status:** pending.
- **Outclass claim:** **Shared core via gRPC + CRDT.**
- **Files:** `sera-mobile/`.
- **Verification:** chat from phone; same session DB.
- **Dependencies:** P-91.

### P-96 — Marketplace

- **Status:** pending.
- **Files:** `marketplace/` (separate repo + Sera CLI integration).
- **Verification:** publish skillpack; another machine installs by name.
- **Dependencies:** P-28, P-87.

### P-97 — Kernel-level integration (optional helpers)

- **Status:** pending.
- **Files:** `sera-helper/` (LaunchAgent + scheduled task + systemd unit).
- **Verification:** hotkey works system-wide.
- **Dependencies:** P-70.

### P-98 — Cross-language interop (Rust hot-paths via PyO3)

- **Status:** pending.
- **Outclass claim:** **Chunker + FTS5 ranker + vector search** in Rust.
- **Files:** `sera-rust/`.
- **Verification:** chunker p99 drops ≥3×.
- **Dependencies:** P-12, P-16.

### P-99 — Public ship (DMG/MSI/deb)

- **Status:** pending.
- **Files:** `installer/`, codesign workflow.
- **Verification:** fresh-machine install + first reply < 5 min.
- **Dependencies:** P-90, P-61.

### P-100 — Sera is in the room with them

- **Status:** pending.
- **Outclass claim:** **Public side-by-side numbers** vs Hermes/OpenHuman/OpenClaw.
- **Goal:** Run a public eval suite; publish numbers; ship comparison page.
- **Verification:** the numbers are out, public, honest.
- **Dependencies:** P-99.

---

## phases/ folder contract

Each phase mirror file in `Project_sera/phases/NN-<slug>.md` keeps:

```markdown
## Status
done | in-progress | pending | blocked | deferred

## Outclass claim
one-line

## Goal
short paragraph

## Deliverables
- bullet
- bullet

## Files touched
- path:line ranges if known

## Verification
```bash
exact command
```
expected output

## Dependencies
P-XX, P-YY

## Notes
journal of decisions, blockers, links to PRs / commits
```

The plan file (this doc) is the source. Phase files are slim summaries that get edited as work progresses. Update via `/save` skill at every phase boundary.

---

## Open decisions (lock before relevant phase)

1. **Council architecture (P-31..P-35).** *Recommended: in-process. Lower latency, one process.*
2. **Voice approach (P-68..P-69).** *Recommended: offline-first via whisper.cpp + piper. Online fallback opt-in.*
3. **Mobile stack (P-95).** *Recommended: Tauri Mobile, shares desktop core directly.*
4. **Local LLM runtime (P-73, P-93).** *Recommended: mlx-lm primary on Apple Silicon; llama.cpp fallback on Linux/Win.*
5. **Marketplace hosting (P-96).** *Recommended: GitHub Pages + signed packs first; full backend later.*

Recommendations are defaults, overridable any time before the phase opens.

---

## Execution rules

1. Phases run linearly within an epoch. Cross-epoch dependencies are minimal and noted.
2. No phase promotes without its verification passing.
3. Every promoted phase ships its outclass claim. Empty outclass = incomplete.
4. Egoist skill loads at every Sera-related session start.
5. `/save` snapshots phase state into `Project_sera/phases/NN-<slug>.md` at every phase boundary.
6. Plan file is the source. Phase files are mirrors.
7. Pacing is deliverable-sized, not calendar-sized.

---

## What's real today (P-01..P-03 done, 2026-05-19)

```
Project_sera/sera/
  __init__.py            v0.1.0
  config.py              ~/.sera/config.yaml loader
  agent/loop.py          ReAct + threshold + approval gate
  llm/base.py            LLM protocol
  llm/router.py          for_profile()
  llm/secrets.py         env > keyring
  llm/adapters/openai_adapter.py
  llm/adapters/anthropic_adapter.py
  tools/base.py          Permission + Tool + Permission.parse
  tools/registry.py      auto-discovery, idempotent
  tools/dispatcher.py    execute()
  tools/impl/file_read.py
  tools/impl/file_write.py
  tools/impl/shell_run.py   + dangerous classifier
  tools/impl/web_search.py  ddgs
  tools/impl/memory_store.py
  memory/session.py      SQLite + FTS5 + _escape_fts5 + current_only
  safety/approval.py     CliApprovalGate + AutoApproveGate
  cli/main.py            chat / setup / tools / sessions / version
tests/
  test_registry.py, test_session.py, test_tools_safe.py,
  test_search.py, test_approval_threshold.py        14 passing
pyproject.toml, README.md
```

Verifications passing today:
```bash
pytest -q                  # 14 passed
sera version               # sera 0.1.0
sera tools                 # 5 tools, tiers correct
sera sessions              # empty or test sessions
sera chat (no key)         # exits 1 with clear hint
```

---

## End-to-end vibe check

If you read top-to-bottom and Sera by P-100 does not stand taller than every rival on this list, the plan is wrong. Specifically:

- By **P-30** skills prove themselves before promotion. Hermes lifecycle-only.
- By **P-50** Sera writes its own tools at runtime. None of the rivals do.
- By **P-70** Sera works on a plane. OpenHuman external-only.
- By **P-80** Sera gets measurably better while you sleep. Nobody else.
- By **P-90** no release ships without the eval gate. Nobody else gates.
- By **P-100** Sera is on devices, in browsers, federated, public.

We are not building a clone with our name on it. We are building the agent that makes the others look like prior art.

— The Egoist
