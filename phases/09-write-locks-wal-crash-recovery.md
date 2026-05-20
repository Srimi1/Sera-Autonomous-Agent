# P-09 — Write locks + WAL + crash recovery

## Status

done (shipped 2026-05-20, this session).

## Outclass claim

**Explicit partial-turn recovery** — on startup, scan for sessions whose last assistant turn has no `finish_reason` recorded; mark them `aborted` and surface in `sera sessions`. Hermes ships WAL; nobody ships explicit partial-turn recovery.

## Goal

Kill -9 mid-turn → restart → session intact + the aborted turn flagged for the user.

## Deliverables

- `sera/memory/session.py`:
  - **WAL probe + DELETE fallback.** `_set_journal_mode` writes `PRAGMA journal_mode=WAL`, reads it back, and falls to `DELETE` if the host filesystem rejected WAL (iCloud Drive, NFS, sshfs). `_warn_wal_fallback_once` emits a one-shot warning via `logging` so the user can see WAL didn't take.
  - **Per-session advisory lock.** `session_lock(session_id)` ctx manager uses `fcntl.flock` on `~/.sera/locks/<session_id>.lock`. Two `sera` processes editing the same session_id serialize; unrelated sessions stay free. No-op on Windows (no `fcntl`); SQLite's own file-level lock still serializes.
  - **`finish_reason` on messages.** Persisted on assistant rows by the loop. NULL means the row was written but the turn never completed cleanly.
  - **`last_status` + `aborted_at` on sessions.** Default `active`. Flipped to `aborted` by `_recover_aborted`.
  - **Recovery scan.** Runs once per DB on first connect (and on every `recover_aborted_sessions()` call). Flags a session if the last message is a dangling `user` row OR an `assistant` row with NULL `finish_reason`. Idempotent — only flips `active` rows.
- `sera/agent/loop.py` — passes `finish_reason` through to the persisted assistant Message.
- `sera/cli/main.py` — `sera sessions` runs the recovery scan first, then shows a `status` column with aborted rows highlighted yellow.

## Files touched

`sera/memory/session.py`, `sera/agent/loop.py`, `sera/cli/main.py`, new `tests/test_crash_recovery.py` (10 tests).

## Verification

```bash
pytest -q tests/test_crash_recovery.py   # 10 passed
pytest -q                                 # 113 passed total (was 103 + 10 new)
# manual: SIGKILL mid-turn; restart; `sera sessions` shows the abort flag
```

## Dependencies

P-02.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-20):**

- **Dual signal for "aborted".** Dangling `user` (mid-stream crash before the assistant row landed) AND dangling `assistant` with NULL `finish_reason` (crashed after row insert but before subsequent commit completed). Both cover real kill paths.
- **No synthetic recovery row.** Some prior art injects a `system` row saying "this session was aborted". Sera doesn't — the `last_status` column is authoritative, the message history stays unmodified, and replay logic (P-14+ memory tree, P-50+ ReAct retries) stays simple.
- **Recovery is on-connect lazy + on-demand.** First `_connect` per DB does it once; `recover_aborted_sessions()` is exposed so the CLI can re-scan without restart. No background daemon, no scheduled job.
- **WAL fallback is silent in practice but logged once.** The `_WAL_WARNED` set keys on path so re-opening the same DB in the same process doesn't spam. iCloud sessions on the project's primary DB *do* hit this path — expected.
- **`session_lock` uses fcntl, not SQLite's `BEGIN IMMEDIATE`.** `BEGIN IMMEDIATE` would lock the entire DB file, blocking every session's writes. Per-session flock files preserve concurrency: session A's writer doesn't block session B's writer at all.
- **Lock files live under `~/.sera/locks/`** (not next to the DB). Keeps the DB directory clean, survives DB moves, and lets `tmp_path`-based tests use shared lock files (the lock is on a known path keyed by session_id, not coupled to DB path — multi-DB sessions with colliding ids would share a lock, which is fine since session ids are uuid4 hex).
- **Init is thread-safe.** `_INIT_LOCK` guards the first-connect block so two threads opening the same DB simultaneously don't race on migrations + recovery. Cheap (held only on first connect per DB per process).
- **No fixed JSON history rewrite.** Recovery doesn't touch the messages table. Session can be loaded, inspected, and even resumed — the user-facing `last_status` is purely informational. Resuming an aborted session is allowed; the dangling user message still feeds the next turn.
