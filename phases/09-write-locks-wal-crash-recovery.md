# P-09 — Write locks + WAL + crash recovery

## Status

pending.

## Outclass claim

**Explicit partial-turn recovery** — on startup, scan for sessions whose last assistant turn has no `finish_reason` recorded; mark them `aborted` and surface in `sera sessions`. Hermes ships WAL; nobody ships explicit partial-turn recovery.

## Goal

Kill -9 mid-turn → restart → session intact + the aborted turn flagged for the user.

## Deliverables

- SQLite `PRAGMA journal_mode=WAL` with `DELETE` fallback for NFS.
  - Advisory lock per session_id (filelock or SQLite `BEGIN IMMEDIATE`) during write.
  - `finish_reason` column on `messages`; recovery scan on startup that flips dangling rows to `aborted`.

## Files touched

`sera/memory/session.py`.

## Verification

```bash
  pytest -q tests/test_crash_recovery.py
  # manual: SIGKILL mid-turn; restart; sera sessions shows the abort flag
  ```

## Dependencies

P-02.


## Notes

_Journal: decisions, blockers, commit refs go here._
