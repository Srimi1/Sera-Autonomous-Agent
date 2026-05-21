# P-23 — Curator background fork

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Cheap-model curator that never blocks the main agent.** A dedicated worker thread picks up `Session`s after the main turn returns; the agent loop never waits on curation. Curator output (`skill_edit`, `memory_note`, `tool_hint` proposals) lands in an append-only SQLite log within seconds. Hermes folds curation into the live turn; Sera pushes it to background, where a fast-tier model belongs.

## Goal

After every >5-tool session, review the trace; propose skill / memory updates.

## Deliverables

- `sera/curator/loop.py`:
  - `tool_call_count(session)` / `should_curate(session, threshold=5)` — pure helpers.
  - `CuratorProposal(kind, payload, reasoning)` + `CuratorReport(session_id, proposals, started_at, finished_at, error?)`.
  - `ALLOWED_PROPOSAL_KINDS = ("skill_edit", "memory_note", "tool_hint")` — closed vocabulary; unknown kinds dropped at parse time.
  - `Curator(llm_call)` — async `review(session)` builds a compact trace, prompts the injected LLM in JSON mode, parses the response into proposals. Provider-agnostic. JSON-parse failures or LLM exceptions produce an empty-proposal report with `.error` populated rather than raising.
  - `CuratorStore(db_path=~/.sera/curator.db)` — append-only SQLite log; `record(report)`, `recent_reports(limit)`. Reports persisted with `proposals_json` so the on-disk shape stays single-table.
  - `CuratorQueue(store, curator_factory, threshold)` — `start()` spawns a daemon worker thread, `enqueue(session)` is non-blocking and threshold-gated, `wait_idle(timeout)` blocks until the queue drains, `stop()` is idempotent. Curator crashes get caught + logged + recorded with `.error` — worker thread never dies.
- `sera/cli/main.py` — `sera curator log [--db PATH] [--limit N]` Rich table (session, when, proposals count, kinds, error).

## Files touched

new `sera/curator/__init__.py`, `sera/curator/loop.py`; edit `sera/cli/main.py`; new `tests/test_curator.py` (18 tests).

## Verification

```bash
pytest -q tests/test_curator.py        # 18 passed
pytest -q                               # 354 passed total (was 336 + 18 new)
python -m pyflakes sera/                # 0 warnings
```

Phase verification clause met: synthetic 10-tool session → curator log entry visible via `recent_reports` within `wait_idle(timeout=5.0)` (test `test_queue_processes_session_in_background`). 60s budget unused.

## Dependencies

P-21, P-22, P-10.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (5 cycles, RED→GREEN each):**

1. RED→GREEN: `tool_call_count` + `should_curate` with threshold semantics (`> threshold`, not `>=`).
2. RED→GREEN: `Curator.review` parses JSON → typed proposals, drops unknown kinds, tolerates malformed JSON, passes tool names in trace.
3. RED→GREEN: `CuratorStore` round-trip, recency ordering, empty handling, error-field persistence.
4. RED→GREEN: `CuratorQueue` background thread — synthetic 10-tool session drains <5s; below-threshold sessions no-op; curator crashes logged with `.error`; double-stop idempotent.
5. RED→GREEN: `sera curator log` CLI lists recent reports; empty-DB notice.

**Design decisions (2026-05-21):**

- **Strict `>` threshold.** Phase doc says `>5`. Test locks it: 5 tool calls → no curate, 6 → curate. Easy to misread as `>=` later — the test makes the contract explicit.
- **Injectable `llm_call`, not a provider name.** Same shape as P-15's `LLMExtractor`. Curator stays decoupled from `sera/llm/` so any fast-tier wrapper (gpt-4o-mini, claude-haiku, local) plugs in without curator changes. The "cheap-model" claim is a config choice, not hard-coded.
- **`Curator.review` never raises.** A curator that takes down the worker thread defeats the whole "never blocks" promise. JSON parse failures, LLM exceptions, and parse-shape mismatches all become `CuratorReport(proposals=(), error="...")`. The store records the failure so post-hoc debugging is one `sera curator log` away.
- **Closed proposal-kind vocabulary.** Same discipline as P-15's `EDGE_KINDS`. Free-text proposal types defeat downstream automation. Unknown kinds dropped at parse time; the LLM gets repeated attempts to match the schema across sessions.
- **`proposals_json` blob, not normalized table.** One row per report keeps the schema flat and lets reports be retrieved + decoded in O(1) per report. A `proposal_items` join table would scale better for billion-row regimes — not the target shape.
- **Daemon thread, not asyncio task.** The agent loop runs its own `asyncio.run`; spawning curator coroutines inside that loop would tangle lifetimes. A standalone thread with its own `asyncio.run` per session is the cleanest isolation — the main loop has zero awareness the worker exists.
- **`wait_idle` is the only sync point.** Production code never calls it; tests do, to synchronize on the worker's completion. The verification clause ("entry within 60s") becomes a deterministic `wait_idle(timeout=5)` assertion rather than `time.sleep + poll`.
- **No automated enqueue from agent loop yet.** P-23 ships the *machinery*. Hooking the agent loop to call `queue.enqueue(session)` on turn completion is a one-liner in P-23.5 or a future phase — out of scope here. Resist horizontal-slice temptation.
- **Worker writes own crash report.** When `curator.review` somehow does raise (shouldn't, but defensive), the worker catches it and records a `CuratorReport(error=str(e))`. The session disappears into the log either way — no silent drops.
- **`_SHUTDOWN` sentinel.** Standard pattern for graceful queue shutdown. The worker's `_run` loop exits on sentinel + sets the idle event so a stuck `wait_idle` after `stop()` returns immediately rather than hanging on timeout.
- **No auto-vacuum / log rotation.** Curator log is append-only; per-row size is ~1KB; even a year of daily 10-report sessions sits at ~3.5MB. Rotation is a P-30+ concern, not skeleton scope.
